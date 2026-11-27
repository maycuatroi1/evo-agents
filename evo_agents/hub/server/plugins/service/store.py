"""The hub's store of plugins: signed bundles by version in the blob store, which version is installed, and the
check at every start that the installed ones still run on this release.

A bundle reaches the store the way a global skill's does: POST /v1/blobs/uploads without a project (kind
plugin-bundle, held by the hub itself, so for hub admins only), the PUT, POST /v1/blobs/commit, then publishing,
which reads the committed bytes back from the blob store and checks them whole (``check_bundle``) before it records a
version: a bundle ``evo_agents.plugins.bundle`` reads, whose manifest names the plugin of the route, with a signature
of its publisher that ``evo_agents.plugins.signing`` accepts against EVO_HUB_PLUGIN_PUBLISHERS, a core plugin only
from a publisher of EVO_HUB_PLUGIN_CORE_PUBLISHERS, and ``engines`` and ``plugin_api`` that this release runs
(``evo_agents.plugins.compat``). Each refusal is a 422 whose message names its reason in parentheses (unsigned,
malformed, altered, untrusted, invalid, incompatible) and what to do; a signature that cannot be checked here
(Sigstore's trust root out of reach) is a 503 and records nothing.

A plugin belongs to the publisher of its first version: a version of another publisher is a 409, as is a version
number published already with other bytes; the same bytes again change nothing. A version is never changed.

Installing or upgrading reads the version's bundle from the blob store again and checks it as publishing did, against
the publishers and the release the hub has now: bytes in the store that are not the ones published are refused
(altered), and so is a publisher the operator no longer trusts. Without a version, install takes the newest version
that runs on this release and upgrade the newest above the installed one. Removing leaves every version in the store.
"""

from __future__ import annotations

import asyncio
import io
import logging

from fastapi import HTTPException
from sqlalchemy import Text, any_, literal, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.blobs import BLOB_PREFIX, BlobStore, BlobStoreUnavailable, blob_key
from evo_agents.hub.config import HubConfig
from evo_agents.hub.server import audit
from evo_agents.hub.server.plugins.models.store import (
    FROM_IMAGE,
    IMAGE,
    UNAVAILABLE,
    Checked,
    Plugin,
    PluginDetail,
    PluginPublishIn,
    PluginVersion,
)
from evo_agents.hub.server.plugins.repository import publishers as publisher_rows
from evo_agents.hub.server.plugins.repository import store as repository
from evo_agents.hub.server.plugins.repository import tokens as token_rows
from evo_agents.hub.server.security import Principal, admin_login
from evo_agents.plugins import compat, signing
from evo_agents.plugins.bundle import MAX_BUNDLE, BundleError, read_bundle
from evo_agents.plugins.environment import MAX_PLUGIN_ID
from evo_agents.plugins.market import merge_publishers
from evo_agents.plugins.signing import SignatureError, SignatureUnavailable

log = logging.getLogger(__name__)


# Checks


def check_bundle(
    data: bytes,
    signature: dict | None,
    config: HubConfig,
    sigstore,
    version: str,
    doing: str,
    publishers: tuple[signing.Publisher, ...] | None = None,
) -> Checked:
    """The manifest of bundle ``data`` and who signed it, or the HTTPException (422, or 503 when the signature cannot
    be checked here) saying why the hub refuses it; ``doing`` ends the message ("nothing was published").
    ``publishers`` are those the hub trusts (``trusted_publishers``), EVO_HUB_PLUGIN_PUBLISHERS alone when None."""
    try:
        manifest = read_bundle(data).manifest
    except BundleError as exc:
        raise HTTPException(422, f"refused (malformed): {exc}; {doing}") from None
    try:
        trusted = config.plugin_publishers if publishers is None else publishers
        signed = signing.verify(data, signature, manifest.publisher, trusted, sigstore)
    except SignatureError as exc:
        raise HTTPException(422, f"refused ({exc.reason}): {exc}; {doing}") from None
    except SignatureUnavailable as exc:
        raise HTTPException(503, f"the signature cannot be checked on this hub now: {exc}; {doing}") from None
    if manifest.tier == "core" and manifest.publisher not in config.plugin_core_publishers:
        raise HTTPException(
            422,
            f"refused (untrusted): {manifest.id} is a core plugin, which runs Python in the hub's processes, and "
            f"publisher {manifest.publisher} is not a core publisher here: an operator lists it in "
            f"EVO_HUB_PLUGIN_CORE_PUBLISHERS; {doing}",
        )
    problem = compat.problem(manifest.engines, manifest.plugin_api, version, manifest.contributes)
    if problem is not None:
        what = f"{manifest.id} {manifest.version}"
        raise HTTPException(422, f"refused (incompatible): {what} does not run on this hub: {problem}; {doing}")
    return Checked(manifest, signed)


async def trusted_publishers(conn: AsyncConnection, config: HubConfig) -> tuple[signing.Publisher, ...]:
    """The publishers whose signatures the hub accepts: EVO_HUB_PLUGIN_PUBLISHERS with the signers hub admins trusted
    and did not remove (``plugin_publishers``, 0154)."""
    return merge_publishers(config.plugin_publishers, await publisher_rows.signers(conn))


async def fetch_bundle(store: BlobStore, sha256: str, size: int) -> bytes:
    """The bytes of a bundle the hub holds, hashed as they are read; 422 (altered) when they are not the ones
    recorded, 503 when the store does not answer or lost them."""
    sink = io.BytesIO()
    try:
        fetched = await asyncio.to_thread(store.fetch, blob_key(sha256), sink, MAX_BUNDLE)
    except BlobStoreUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None
    if fetched is None:
        log.error("a plugin bundle is missing from the blob store", extra={"sha256": sha256})
        raise HTTPException(503, f"the blob store does not hold bundle {sha256}; nothing changed, tell a hub admin")
    if fetched != (sha256, size):
        log.error("a plugin bundle in the blob store is not the one recorded", extra={"sha256": sha256})
        raise HTTPException(
            422,
            f"refused (altered): the blob store holds other bytes than bundle {sha256} ({fetched[1]} bytes, SHA-256 "
            f"{fetched[0][:16]}...); nothing changed, tell a hub admin",
        )
    return sink.getvalue()


# Reading


def version_model(row, hub_version: str) -> PluginVersion:
    manifest = row.manifest
    problem = compat.problem(manifest["engines"], manifest["plugin_api"], hub_version, manifest.get("contributes"))
    return PluginVersion(
        version=row.version,
        sha256=row.sha256,
        size=row.size,
        publisher=row.publisher,
        tier=row.tier,
        plugin_api=manifest["plugin_api"],
        engines=manifest["engines"]["evo-agents"],
        permissions=manifest.get("permissions") or [],
        signature_kind=row.signature_kind,
        signed_by=row.signed_by,
        compatible=problem is None,
        incompatible_reason=problem,
        published_by=row.published_by,
        published_at=row.published_at,
    )


def _upgrade_to(installed: str | None, versions: list[PluginVersion]) -> str | None:
    if installed is None:
        return None
    floor = compat.plugin_version_key(installed)
    newer = [v for v in versions if v.compatible and compat.plugin_version_key(v.version) > floor]
    return newer[0].version if newer else None


def shown_version(row, rows: list):
    """The version row whose manifest speaks for plugin ``row``: the installed one, else the newest of ``rows``."""
    return next((v for v in rows if v.version == row.installed_version), rows[0])


def plugin_model(row, versions: list[PluginVersion], manifest: dict) -> dict:
    """The fields of Plugin for ``row`` of the plugins query, its versions, newest first, and the manifest of the
    version shown (``shown_version``)."""
    state = "available" if row.installed_version is None else "disabled" if row.disabled_reason else "installed"
    return {
        "name": row.name,
        "title": manifest.get("name") or row.name,
        "description": manifest.get("description") or "",
        "tier": manifest.get("tier", "isolated"),
        "publisher": row.publisher,
        "state": state,
        "installed_version": row.installed_version,
        "installed_by": row.installed_by,
        "installed_at": row.installed_at,
        "disabled_reason": row.disabled_reason,
        "disabled_at": row.disabled_at,
        "latest_version": versions[0].version,
        "upgrade_to": _upgrade_to(row.installed_version, versions),
        "project": row.project,
        "created_at": row.created_at,
    }


async def list_plugins(conn: AsyncConnection, hub_version: str) -> list[Plugin]:
    """The plugins of the store; the features of the hub (``plugins.builtin``: canvas, srs, tasks) are none of them."""
    rows, by_plugin = await repository.store_rows(conn)
    found = []
    for row in rows:
        held = by_plugin.get(row.id, [])
        if held:
            versions = [version_model(v, hub_version) for v in held]
            found.append(Plugin(**plugin_model(row, versions, shown_version(row, held).manifest)))
    return found


def not_found(name: str) -> HTTPException:
    return HTTPException(404, f"no plugin {name} on this hub: see `evo-agents hub plugin list`")


async def detail(conn: AsyncConnection, name: str, hub_version: str) -> PluginDetail:
    row = await repository.find(conn, name)
    rows = [] if row is None else await repository.version_rows(conn, row.id)
    if not rows:
        raise not_found(name)
    versions = [version_model(v, hub_version) for v in rows]
    shown = shown_version(row, rows)
    return PluginDetail(**plugin_model(row, versions, shown.manifest), manifest=shown.manifest, versions=versions)


async def plugin(conn: AsyncConnection, name: str, hub_version: str) -> Plugin:
    found = await detail(conn, name, hub_version)
    return Plugin(**found.model_dump(include=set(Plugin.model_fields)))


# Publishing


async def committed(conn: AsyncConnection, sha256: str, size: int) -> None:
    """422 unless the hub itself holds the committed bundle with the declared size."""
    held = await repository.committed_size(conn, sha256)
    if held is None:
        raise HTTPException(
            422,
            f"bundle {sha256} is not committed for the hub: ask for an upload without a project (POST "
            "/v1/blobs/uploads, kind plugin-bundle), PUT it and commit it first; nothing was published",
        )
    if held != size:
        raise HTTPException(422, f"bundle {sha256} has {held} bytes, not the {size} declared; nothing was published")


async def held_version(conn: AsyncConnection, name: str, sha256: str):
    """The version of plugin ``name`` whose bundle is ``sha256``, or None."""
    row = await repository.find(conn, name)
    if row is None:
        return None
    return next((v for v in await repository.version_rows(conn, row.id) if v.sha256 == sha256), None)


async def record_version(conn: AsyncConnection, user: Principal, body: PluginPublishIn, checked: Checked) -> bool:
    """Write the version ``checked`` describes, creating its plugin; False when it is there already. 409 for a
    plugin of another publisher, or a version number published with other bytes."""
    manifest = checked.manifest
    await repository.add_plugin(conn, manifest.id, manifest.publisher)
    row = await repository.find(
        conn, manifest.id, lock=True, builtin=True
    )  # versions of one plugin are written one at a time
    versions = await repository.version_rows(conn, row.id)  # pyright: ignore[reportOptionalMemberAccess]
    if from_image(versions):
        raise HTTPException(409, FROM_IMAGE.format(name=manifest.id))
    if row.publisher != manifest.publisher:  # pyright: ignore[reportOptionalMemberAccess]
        raise HTTPException(
            409,
            f"plugin {manifest.id} is published by {row.publisher}; publisher {manifest.publisher} cannot publish a "  # pyright: ignore[reportOptionalMemberAccess]
            "version of it",
        )
    same = next((v for v in versions if v.version == manifest.version), None)
    if same is not None:
        if same.sha256 == body.sha256:
            return False
        raise HTTPException(
            409,
            f"version {manifest.version} of plugin {manifest.id} is published with another bundle; a version never "
            "changes, publish the change as a new version",
        )
    await repository.add_version(
        conn,
        {
            "plugin_id": row.id,  # pyright: ignore[reportOptionalMemberAccess]
            "version": manifest.version,
            "sha256": body.sha256,
            "size": body.size,
            "r2_key": BLOB_PREFIX + body.sha256,
            "publisher": manifest.publisher,
            "tier": manifest.tier,
            "manifest": manifest.data,
            "signature": body.signature,
            "signature_kind": checked.signed.kind,
            "signed_by": checked.signed.signed_by,
            "published_by": user.user_id,
        },
    )
    target = f"plugin:{manifest.id}@{manifest.version}"
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.PLUGIN_PUBLISH, target=target)
    return True


# Installing


def from_image(versions: list) -> bool:
    """Whether a plugin, by its versions, is a core plugin of the hub's image, which the store does not install."""
    return any(version.signature_kind == IMAGE for version in versions)


def pick_version(name: str, rows: list, wanted: str | None, floor: str | None, hub_version: str):
    """The version row to install: ``wanted``, or the newest that runs on this hub (above ``floor`` when given)."""
    if wanted is not None:
        found = next((v for v in rows if v.version == wanted), None)
        if found is None:
            raise HTTPException(404, f"plugin {name} has no version {wanted}: see `evo-agents hub plugin show {name}`")
        return found
    above = None if floor is None else compat.plugin_version_key(floor)
    for row in rows:
        newer = above is None or compat.plugin_version_key(row.version) > above
        if newer and version_model(row, hub_version).compatible:
            return row
    what = "no version" if floor is None else "no version above the installed one"
    raise HTTPException(409, f"plugin {name} has {what} that runs on this hub: see `evo-agents hub plugin show {name}`")


def check_state(row, *, upgrade: bool, target: str | None = None) -> None:
    """409 when plugin ``row`` cannot be installed (or upgraded to ``target``) as it stands."""
    installed = row.installed_version
    if upgrade and installed is None:
        raise HTTPException(409, f"plugin {row.name} is not installed: install it with `evo-agents hub plugin install`")
    if not upgrade and installed is not None and row.disabled_reason is None:
        raise HTTPException(
            409, f"plugin {row.name} is installed at {installed}: move it with `evo-agents hub plugin upgrade`"
        )
    if upgrade and target is not None and compat.plugin_version_key(target) <= compat.plugin_version_key(installed):  # pyright: ignore[reportArgumentType]
        raise HTTPException(409, f"{target} is not above {installed}, the installed version of plugin {row.name}")


def check_placeable(row, version) -> None:
    """409 when the plugin host could not run the backend of ``version`` of plugin ``row``: an isolated plugin with a
    backend whose id on the hub is MAX_PLUGIN_ID or more, for which the host has no killer user of its own
    (``evo_agents.plugins.environment.killer_uid``) and starts nothing."""
    if version.tier == "isolated" and (version.manifest or {}).get("backend") and row.id >= MAX_PLUGIN_ID:
        raise HTTPException(
            409,
            f"plugin {row.name} has id {row.id} on this hub, and the plugin host runs the backend only of a plugin "
            f"whose id is below {MAX_PLUGIN_ID}",
        )


async def set_installed(
    conn: AsyncConnection, user: Principal, row, version: str, *, upgrade: bool, project_id: int | None = None
) -> None:
    """Install ``version`` of plugin ``row``, turned on, with its audit row, in project ``project_id`` when an admin of
    that self-managed project does it."""
    await repository.set_installed(conn, row.id, version, user.user_id)
    action = audit.PLUGIN_UPGRADE if upgrade else audit.PLUGIN_INSTALL
    target = f"plugin:{row.name}@{version}"
    await audit.record(
        conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target, project_id=project_id
    )


async def set_removed(conn: AsyncConnection, user: Principal, row, *, project_id: int | None = None) -> None:
    await repository.set_removed(conn, row.id)
    await token_rows.revoke_tokens(conn, row.id)  # its backend's token stops working now, not when the host next pulls
    target = f"plugin:{row.name}@{row.installed_version}"
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.PLUGIN_REMOVE,
        target=target,
        project_id=project_id,
    )


# The hub admins a notice goes to: the query reads ``security.admin_login``, which the repository may not import


async def hub_admin_ids(conn: AsyncConnection, admins: frozenset[str]) -> list[int]:
    """The ids of the hub admins (EVO_HUB_ADMINS) who have signed in, by id."""
    users = tables.users
    logins = literal(sorted(admins), ARRAY(Text))
    query = select(users.c.id).where(admin_login(users) == any_(logins)).order_by(users.c.id)
    return list((await conn.execute(query)).scalars())
