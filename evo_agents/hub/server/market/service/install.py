"""Installing a free plugin from a market or a GitHub release (plan plugin-market, criterion a6), and a project
admin's request that a hub admin install one.

- POST /v1/market/{source}/{plugin}/install (hub admins): install the latest release of a market's free isolated
  plugin, or ``version``, or upgrade the installed one to it;
- POST /v1/plugins/install-github (hub admins): the same from the release of a GitHub repo, ``OWNER/REPO`` and a tag
  or the latest, whose signer the admin names (``sigstore:OWNER/REPO/.github/workflows/ci.yml`` by default, as the
  plugin template signs);
- POST /v1/market/{source}/{plugin}/request (a project's admins): ask the hub admins to install it; each gets a notice
  ``market_install_request`` linking to the plugin's page in the market.

An install downloads the release's bundle ``<id>-<version>.tar.gz`` and its signature ``<id>-<version>.tar.gz
.sigstore.json`` (or ``.sig.json``) from GitHub, checks that the bundle is the entry's plugin at the release's version
by the entry's publisher and that the signer the market names signed it (``evo_agents.plugins.market``). A signer the
hub does not trust for that publisher yet is a 409 unless the admin confirms it (``trust_publisher``): the hub then
trusts it from now on (``plugin_publishers``, audited as plugin.publisher.add), unless EVO_HUB_PLUGIN_PUBLISHERS_LOCKED
or the publisher is a core publisher. Then it runs the store's own path: the bundle into the blob store, checked again
against the publishers it now trusts and this release (``plugin_store.check_bundle``), recorded as a version
(plugin.publish) and installed or upgraded (plugin.install, plugin.upgrade). A core plugin is never installed from a
market: it ships in the hub's image. The open requests of the plugin are marked installed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.config import HubConfig
from evo_agents.hub.market_refresh import GitHubMarket, MarketUnavailable
from evo_agents.hub.self_managed import ProjectScope
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.errors import Conflict, Unprocessable
from evo_agents.hub.server.blobs import blob_store, put_bytes
from evo_agents.hub.server.market.models.install import (
    BUNDLE_ASSET,
    NOTHING_INSTALLED,
    NOTICE,
    GitHubInstallIn,
    MarketInstalled,
    MarketInstallIn,
    MarketRequest,
    MarketRequestIn,
)
from evo_agents.hub.server.market.repository import install as repository
from evo_agents.hub.server.market.service.market import entry_of, source_row
from evo_agents.hub.server.notifications.service.notifier import NOTIFIER
from evo_agents.hub.server.plugins.models.store import PluginPublishIn
from evo_agents.hub.server.plugins.repository.store import find, version_rows
from evo_agents.hub.server.plugins.service.grants import check_admin
from evo_agents.hub.server.plugins.service.publishers import check_may_trust, trust
from evo_agents.hub.server.plugins.service.store import (
    check_bundle,
    check_placeable,
    hub_admin_ids,
    record_version,
    set_installed,
    trusted_publishers,
)
from evo_agents.hub.server.plugins.service.store import plugin as stored_plugin
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import Principal
from evo_agents.plugins.bundle import BundleError, read_bundle
from evo_agents.plugins.compat import plugin_version_key
from evo_agents.plugins.market import (
    CheckedRelease,
    ReleaseError,
    check_release,
    default_signer,
    merge_publishers,
    release_assets,
    trusts,
)
from evo_agents.plugins.signing import SignatureError, SignatureUnavailable

log = logging.getLogger(__name__)

# Downloading


def _refused(exc: Exception) -> HTTPException:
    if isinstance(exc, SignatureError):
        return Unprocessable(f"refused ({exc.reason}): {exc}; {NOTHING_INSTALLED}")
    if isinstance(exc, SignatureUnavailable):
        return HTTPException(503, f"the signature cannot be checked on this hub now: {exc}; {NOTHING_INSTALLED}")
    if isinstance(exc, MarketUnavailable):
        return HTTPException(502, f"{exc}; {NOTHING_INSTALLED}")
    return Unprocessable(f"refused: {exc}; {NOTHING_INSTALLED}")


def _release(reader: GitHubMarket, repo: str, version: str | None) -> dict:
    tags = [None] if version is None else [f"v{version}", version]
    for tag in tags:
        found = reader.release(repo, tag)
        if found is not None:
            return found
    raise ReleaseError(f"{repo} has no release" + (f" of version {version}" if version else ""))


def download_market(reader: GitHubMarket, item: dict, version: str | None, sigstore) -> CheckedRelease:
    """The release of market entry ``item``, downloaded and checked against what the market says."""
    assets = release_assets(item["id"], _release(reader, item["repo"], version))
    return check_release(item, assets, reader.download(assets.bundle), reader.download(assets.signature), sigstore)


def download_github(reader: GitHubMarket, body: GitHubInstallIn, sigstore) -> tuple[dict, CheckedRelease]:
    """The release of a GitHub repo, as a market entry the admin's signer stands for, downloaded and checked."""
    release = reader.release(body.repo, body.tag)
    if release is None:
        raise ReleaseError(f"{body.repo} has no release" + (f" tagged {body.tag}" if body.tag else ""))
    names = [BUNDLE_ASSET.fullmatch(str(asset.get("name"))) for asset in release.get("assets") or ()]
    found = [match[1] for match in names if match]
    if len(set(found)) != 1:
        raise ReleaseError(
            f"release {release.get('tag_name')} of {body.repo} carries no single bundle ID-VERSION.tar.gz"
        )
    assets = release_assets(found[0], release)
    data = reader.download(assets.bundle)
    try:
        manifest = read_bundle(data).manifest
    except BundleError as exc:
        raise ReleaseError(f"{assets.bundle.get('name')}: {exc}") from None
    signer = body.signer or default_signer(body.repo)
    item = {"id": manifest.id, "repo": body.repo, "publisher": {"id": manifest.publisher, "signer": signer}}
    return item, check_release(item, assets, data, reader.download(assets.signature), sigstore)


async def _fetched(config: HubConfig, transport, sigstore, fetch, *args):
    """What ``fetch`` reads of GitHub through ``transport`` (the real GitHub when None); HTTPException when it is
    refused."""
    reader = GitHubMarket(config, transport=transport)
    try:
        return await asyncio.to_thread(fetch, reader, *args, sigstore)
    except (ReleaseError, SignatureError, SignatureUnavailable, MarketUnavailable) as exc:
        raise _refused(exc) from None
    finally:
        reader.close()


async def _download(request: Request, fetch, *args):
    state = request.app.state
    return await _fetched(state.config, getattr(state, "market_transport", None), state.plugin_sigstore, fetch, *args)


# Installing


async def check_trust(
    engine: AsyncEngine, config: HubConfig, publisher: str, signer: str, confirmed: bool, *, for_one: bool = False
) -> tuple[tuple, bool]:
    """The publishers the hub trusts with ``signer`` for ``publisher``, and whether that signer is new to it; 409 when
    it is new and not ``confirmed``. For an admin of a self-managed project (``for_one``) the signer is trusted for this
    release alone and the hub keeps no trust of it: the answer says it is not new."""
    async with engine.begin() as conn:
        trusted = await trusted_publishers(conn, config)
    if trusts(trusted, publisher, signer):
        return trusted, False
    check_may_trust(config, publisher)
    if for_one:
        return merge_publishers(trusted, [(publisher, signer)]), False
    if not confirmed:
        raise Conflict(
            f"untrusted publisher: this hub does not trust {signer} to sign the plugins of {publisher}; confirm it "
            f"(trust_publisher, --trust) to trust it from now on; {NOTHING_INSTALLED}",
        )
    return merge_publishers(trusted, [(publisher, signer)]), True


async def _place(conn: AsyncConnection, user, name: str, version: str, project_id: int | None = None) -> bool:
    """Install or upgrade plugin ``name`` to ``version``; whether it was an upgrade. 409 for an older version."""
    row = await find(conn, name, lock=True)
    target = next(v for v in await version_rows(conn, row.id) if v.version == version)  # pyright: ignore[reportOptionalMemberAccess]
    check_placeable(row, target)
    installed = row.installed_version  # pyright: ignore[reportOptionalMemberAccess]
    if installed is not None and row.disabled_reason is None:  # pyright: ignore[reportOptionalMemberAccess]
        if plugin_version_key(version) < plugin_version_key(installed):
            raise Conflict(f"plugin {name} is installed at {installed}, above {version}; {NOTHING_INSTALLED}")
        if version == installed:
            return False
        await set_installed(conn, user, row, version, upgrade=True, project_id=project_id)
        return True
    await set_installed(conn, user, row, version, upgrade=False, project_id=project_id)
    return False


@dataclass(frozen=True)
class Installing:
    """What installing a checked release needs, without a request: the hub's engine, config, signature checker,
    version and blob store (``evo_agents.hub.server.market.service.licenses`` installs a paid plugin from the
    worker)."""

    engine: AsyncEngine
    config: HubConfig
    sigstore: object
    hub_version: str
    store: BlobStore

    @classmethod
    def of(cls, request: Request) -> Installing:
        state = request.app.state
        return cls(state.engine, state.config, state.plugin_sigstore, state.hub_version, blob_store(request))


async def _scope_plugin(conn: AsyncConnection, name: str, scope: ProjectScope) -> bool:
    """Refuse a plugin ``name`` the hub or another project holds; whether the store holds none of that name yet."""
    row = await find(conn, name, lock=True)
    if row is None:
        return True
    if row.project_id != scope.project_id:
        whose = "the hub's" if row.project_id is None else "another project's"
        raise Conflict(
            f"plugin {name} is {whose} already: a self-managed project installs only its own; {NOTHING_INSTALLED}"
        )
    return False


async def install_checked(
    hub: Installing,
    user,
    item: dict,
    checked: CheckedRelease,
    confirmed: bool,
    scope: ProjectScope | None = None,
):
    """Trust the entry's signer when confirmed, then publish and install the checked release as the store does. For an
    admin of a self-managed project (``scope``) the plugin is that project's alone and the signer is trusted for this
    release only."""
    manifest = checked.manifest
    if manifest.tier != "isolated":
        raise Unprocessable(
            f"{manifest.id} is a core plugin, which ships in the hub's image and is never installed from a market"
        )
    publisher, signer = item["publisher"]["id"], item["publisher"]["signer"]
    publishers, new = await check_trust(hub.engine, hub.config, publisher, signer, confirmed, for_one=scope is not None)
    data = checked.data
    stored = await asyncio.to_thread(
        check_bundle,
        data,
        checked.signature,
        hub.config,
        hub.sigstore,
        hub.hub_version,
        NOTHING_INSTALLED,
        publishers,
    )
    sha256 = hashlib.sha256(data).hexdigest()
    await asyncio.to_thread(put_bytes, hub.store, sha256, data)
    body = PluginPublishIn(sha256=sha256, size=len(data), signature=checked.signature)
    async with hub.engine.begin() as conn:
        if new:
            await trust(conn, user, hub.config, publisher, signer, f"installing {manifest.id} {manifest.version}")
        await repository.add_bundle_blob(conn, sha256, len(data), user.user_id)
        fresh = scope is not None and await _scope_plugin(conn, manifest.id, scope)
        await record_version(conn, user, body, stored)
        if fresh:
            await repository.set_plugin_project(conn, manifest.id, scope.project_id)  # pyright: ignore[reportOptionalMemberAccess]
        upgraded = await _place(conn, user, manifest.id, manifest.version, scope.project_id if scope else None)
        await repository.close_requests(conn, user.user_id, manifest.id)
        result = await stored_plugin(conn, manifest.id, hub.hub_version)
    log.info("plugin installed from a release", extra={"plugin": manifest.id, "version": manifest.version})
    return MarketInstalled(plugin=result, trusted_publisher=new, upgraded=upgraded)


async def install_github(
    hub: Installing, transport, user, body: GitHubInstallIn, scope: ProjectScope | None = None
) -> MarketInstalled:
    """Install the release of a GitHub repo ``body`` names, as ``install_from_github`` does, without a request: what
    approving a proposal of @builder to install a plugin does (``evo_agents.core_plugins.builder``). ``transport``
    reads GitHub, the real one when None."""
    item, checked = await _fetched(hub.config, transport, hub.sigstore, download_github, body)
    return await install_checked(hub, user, item, checked, body.trust_publisher, scope)


async def install_release(request: Request, user, item: dict, checked: CheckedRelease, confirmed: bool):
    """``install_checked`` with what the request's app holds."""
    return await install_checked(Installing.of(request), user, item, checked, confirmed)


async def install_from_market(
    request: Request, source: int, plugin: str, body: MarketInstallIn, user: Principal
) -> MarketInstalled:
    """Install free isolated plugin ``plugin`` of market ``source``, or upgrade it, from its GitHub release."""
    async with request.app.state.engine.begin() as conn:
        item = entry_of(await source_row(conn, source), plugin)
    if item["tier"] != "isolated":
        raise Conflict(f"{plugin} is a core plugin: it ships in the hub's image, which the operator upgrades")
    if item["price"]["kind"] != "free":
        raise Conflict(f"{plugin} is sold by subscription: it needs a licence of its seller first")
    checked = await _download(request, download_market, item, body.version)
    return await install_release(request, user, item, checked, body.trust_publisher)


async def install_from_github(request: Request, body: GitHubInstallIn, user: Principal) -> MarketInstalled:
    """Install the release of the GitHub repo ``body`` names, with what the request's app holds."""
    item, checked = await _download(request, download_github, body)
    return await install_release(request, user, item, checked, body.trust_publisher)


# Asking


async def request_install(
    conn: AsyncConnection, config: HubConfig, source: int, plugin: str, body: MarketRequestIn, user: Principal
) -> MarketRequest:
    """Ask the hub admins to install plugin ``plugin`` of market ``source`` for project ``body.project``, of which
    ``user`` is an admin; each hub admin gets a notice, once per open request."""
    access = await project_access(conn, user, body.project)
    check_admin(access, "asking for a plugin")
    row = await source_row(conn, source)
    item = entry_of(row, plugin)
    found = await repository.add_request(conn, access.project_id, source, plugin, user.user_id)
    if found is None:
        open_one = await repository.open_request(conn, access.project_id, source, plugin)
        return MarketRequest(id=open_one.id, project=body.project, plugin=plugin, state="open", created=False)  # pyright: ignore[reportOptionalMemberAccess]
    target = f"{body.project}/market:{source}/{plugin}"
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.MARKET_REQUEST,
        target=target,
        project_id=access.project_id,
    )
    for admin_id in await hub_admin_ids(conn, config.admins):
        await NOTIFIER.notify(
            conn,
            user_id=admin_id,
            kind="notice",
            title=f"{user.login} asks for {item['name']} in {body.project}",
            body=f"{item['name']} ({plugin}) of market {row.repo}, by {item['publisher']['id']}",
            notice_kind=NOTICE,
            # Of no project: a hub admin need hold no grant on it to read it (the Inbox shows a project's notices
            # to its members only); the project is in the title and the details.
            details={"source": source, "plugin": plugin, "project": body.project},
            link=f"/market/{source}/{plugin}",
        )
    return MarketRequest(id=found.id, project=body.project, plugin=plugin, state=found.state, created=True)
