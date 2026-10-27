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
import re
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import tables
from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.config import HubConfig
from evo_agents.hub.market_refresh import GitHubMarket, MarketUnavailable
from evo_agents.hub.self_managed import ProjectScope
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.blobs import blob_store, put_bytes
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.market import PluginId, SourceId, entry_of, source_row
from evo_agents.hub.server.notifications import notify
from evo_agents.hub.server.plugin_grants import check_admin
from evo_agents.hub.server.plugin_publishers import SIGNER, check_may_trust, trust
from evo_agents.hub.server.plugin_store import (
    Plugin,
    PluginPublishIn,
    check_bundle,
    check_placeable,
    find,
    hub_admin_ids,
    record_version,
    set_installed,
    trusted_publishers,
    version_rows,
)
from evo_agents.hub.server.plugin_store import (
    plugin as stored_plugin,
)
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import AdminUser, CurrentUser, admin
from evo_agents.plugins.bundle import BundleError, read_bundle
from evo_agents.plugins.compat import plugin_version_key
from evo_agents.plugins.market import (
    REPO,
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

NOTICE = "market_install_request"  # evo_agents.hub.runs.NOTICE_KINDS
NOTHING_INSTALLED = "nothing was installed"
VERSION = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]{1,40})?$"
TAG = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
BUNDLE_ASSET = re.compile(r"([a-z][a-z0-9-]{0,62})-(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]{1,40})?)\.tar\.gz")
REFUSALS = {code: {"model": ErrorBody} for code in (404, 409, 422, 502, 503)}

router = APIRouter(prefix="/v1/market", tags=["market"], responses={401: {"model": ErrorBody}})
github_router = APIRouter(
    prefix="/v1/plugins",
    tags=["plugins"],
    dependencies=[Depends(admin)],
    responses={401: {"model": ErrorBody}, 403: {"model": ErrorBody}},
)


class MarketInstallIn(BaseModel):
    version: Annotated[str | None, Field(pattern=VERSION)] = Field(
        None, description="the version to install; the latest release when left out"
    )
    trust_publisher: bool = Field(
        False, description="trust the signer for the publisher from now on, when the hub does not yet"
    )


class GitHubInstallIn(BaseModel):
    repo: Annotated[str, Field(pattern=f"^{REPO.pattern}$", description="OWNER/REPO")]
    tag: Annotated[str | None, Field(pattern=TAG)] = Field(
        None, description="the release's tag; the latest if left out"
    )
    signer: Annotated[str | None, Field(pattern=SIGNER)] = Field(
        None, description="what signs the releases; sigstore:OWNER/REPO/.github/workflows/ci.yml when left out"
    )
    trust_publisher: bool = False


class MarketInstalled(BaseModel):
    plugin: Plugin
    trusted_publisher: bool = Field(description="whether this install made the hub trust the publisher's signer")
    upgraded: bool


class MarketRequestIn(BaseModel):
    project: Annotated[str, Field(pattern=PROJECT_NAME)]


class MarketRequest(BaseModel):
    id: int
    project: str
    plugin: str
    state: str
    created: bool = Field(description="False when the project had asked already and the request is still open")


# Downloading


def _refused(exc: Exception) -> HTTPException:
    if isinstance(exc, SignatureError):
        return HTTPException(422, f"refused ({exc.reason}): {exc}; {NOTHING_INSTALLED}")
    if isinstance(exc, SignatureUnavailable):
        return HTTPException(503, f"the signature cannot be checked on this hub now: {exc}; {NOTHING_INSTALLED}")
    if isinstance(exc, MarketUnavailable):
        return HTTPException(502, f"{exc}; {NOTHING_INSTALLED}")
    return HTTPException(422, f"refused: {exc}; {NOTHING_INSTALLED}")


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
        raise HTTPException(
            409,
            f"untrusted publisher: this hub does not trust {signer} to sign the plugins of {publisher}; confirm it "
            f"(trust_publisher, --trust) to trust it from now on; {NOTHING_INSTALLED}",
        )
    return merge_publishers(trusted, [(publisher, signer)]), True


async def _place(conn: AsyncConnection, user, name: str, version: str, project_id: int | None = None) -> bool:
    """Install or upgrade plugin ``name`` to ``version``; whether it was an upgrade. 409 for an older version."""
    row = await find(conn, name, lock=True)
    target = next(v for v in await version_rows(conn, row.id) if v.version == version)
    check_placeable(row, target)
    installed = row.installed_version
    if installed is not None and row.disabled_reason is None:
        if plugin_version_key(version) < plugin_version_key(installed):
            raise HTTPException(409, f"plugin {name} is installed at {installed}, above {version}; {NOTHING_INSTALLED}")
        if version == installed:
            return False
        await set_installed(conn, user, row, version, upgrade=True, project_id=project_id)
        return True
    await set_installed(conn, user, row, version, upgrade=False, project_id=project_id)
    return False


async def _close_requests(conn: AsyncConnection, user, name: str) -> None:
    r = tables.market_install_requests
    await conn.execute(
        update(r)
        .where(r.c.plugin == name, r.c.state == "open")
        .values(state="installed", decided_by=user.user_id, decided_at=func.now())
    )


@dataclass(frozen=True)
class Installing:
    """What installing a checked release needs, without a request: the hub's engine, config, signature checker,
    version and blob store (``evo_agents.hub.server.market_licenses`` installs a paid plugin from the worker)."""

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
        raise HTTPException(
            409, f"plugin {name} is {whose} already: a self-managed project installs only its own; {NOTHING_INSTALLED}"
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
        raise HTTPException(
            422, f"{manifest.id} is a core plugin, which ships in the hub's image and is never installed from a market"
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
        blobs = tables.blobs
        await conn.execute(
            pg_insert(blobs)
            .values(project_id=None, sha256=sha256, size=len(data), kind="plugin-bundle", created_by=user.user_id)
            .on_conflict_do_nothing()
        )
        fresh = scope is not None and await _scope_plugin(conn, manifest.id, scope)
        await record_version(conn, user, body, stored)
        if fresh:
            plugins = tables.plugins
            await conn.execute(update(plugins).where(plugins.c.name == manifest.id).values(project_id=scope.project_id))
        upgraded = await _place(conn, user, manifest.id, manifest.version, scope.project_id if scope else None)
        await _close_requests(conn, user, manifest.id)
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


@router.post("/{source}/{plugin}/install", response_model=MarketInstalled, responses=REFUSALS)
async def install_from_market(
    request: Request, source: SourceId, plugin: PluginId, body: MarketInstallIn, user: AdminUser
) -> MarketInstalled:
    """Install a free plugin of a market, or upgrade it, from its GitHub release."""
    async with request.app.state.engine.begin() as conn:
        item = entry_of(await source_row(conn, source), plugin)
    if item["tier"] != "isolated":
        raise HTTPException(409, f"{plugin} is a core plugin: it ships in the hub's image, which the operator upgrades")
    if item["price"]["kind"] != "free":
        raise HTTPException(409, f"{plugin} is sold by subscription: it needs a licence of its seller first")
    checked = await _download(request, download_market, item, body.version)
    return await install_release(request, user, item, checked, body.trust_publisher)


@github_router.post("/install-github", response_model=MarketInstalled, responses=REFUSALS)
async def install_from_github(request: Request, body: GitHubInstallIn, user: AdminUser) -> MarketInstalled:
    """Install a plugin from the release of a GitHub repo, signed by the signer named (or the repo's ci.yml)."""
    item, checked = await _download(request, download_github, body)
    return await install_release(request, user, item, checked, body.trust_publisher)


# Asking


@router.post("/{source}/{plugin}/request", response_model=MarketRequest, responses=REFUSALS)
async def request_install(
    request: Request, source: SourceId, plugin: PluginId, body: MarketRequestIn, user: CurrentUser
) -> MarketRequest:
    """Ask the hub admins to install a plugin of a market for project ``project``, of which the caller is an admin."""
    config = request.app.state.config
    r = tables.market_install_requests
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, body.project)
        check_admin(access, "asking for a plugin")
        row = await source_row(conn, source)
        item = entry_of(row, plugin)
        added = await conn.execute(
            pg_insert(r)
            .values(project_id=access.project_id, source_id=source, plugin=plugin, requested_by=user.user_id)
            .on_conflict_do_nothing()
            .returning(r.c.id, r.c.state)
        )
        found = added.first()
        if found is None:
            held = select(r.c.id, r.c.state).where(
                r.c.project_id == access.project_id, r.c.source_id == source, r.c.plugin == plugin, r.c.state == "open"
            )
            open_one = (await conn.execute(held)).first()
            return MarketRequest(id=open_one.id, project=body.project, plugin=plugin, state="open", created=False)
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
            await notify(
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
