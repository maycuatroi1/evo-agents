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

A core plugin that comes with the hub's image (``register_core_plugins``, as the api starts) is recorded as a version
of signature kind ``image``, published and installed by nobody, which the store neither installs, upgrades nor removes
(409): the image does. It is recorded only when the hub's schema has every table it owns. One that projects enable
(``Manifest.per_project``) is then enabled per project as any other, and its install is audited; one that runs hub-wide
(Telegram) is listed here and nowhere in a project (``evo_agents.hub.server.plugin_grants``), with no audit row: it ran
before the store listed it, and nobody did anything.

``disable_incompatible`` runs as the api starts: an installed plugin whose version no longer runs on this release
(the hub was upgraded past its ``engines``) is turned off with the reason, an audit row and a notice
``plugin_disabled`` to every hub admin who has signed in, and the hub serves on. Installing it again, or upgrading it
to a version that runs, turns it back on. A core plugin of the image is not among them: the image loads it only on a
release it runs on, and records it as it starts.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import Text, any_, func, insert, literal, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import tables
from evo_agents.hub.blobs import BLOB_PREFIX, BlobStore, BlobStoreUnavailable, blob_key
from evo_agents.hub.config import HubConfig
from evo_agents.hub.server import audit
from evo_agents.hub.server.notifications import notify
from evo_agents.hub.server.plugin_tokens import revoke_tokens
from evo_agents.hub.server.security import Principal
from evo_agents.plugins import compat, signing
from evo_agents.plugins.bundle import MAX_BUNDLE, BundleError, read_bundle
from evo_agents.plugins.environment import MAX_PLUGIN_ID
from evo_agents.plugins.manifest import PLUGIN_ID, TIERS, Manifest, is_per_project
from evo_agents.plugins.market import merge_publishers
from evo_agents.plugins.signing import MAX_SIGNATURE, SIGNATURE_KINDS, SignatureError, SignatureUnavailable

log = logging.getLogger(__name__)

STATES = ("available", "installed", "disabled")
NOTICE = "plugin_disabled"  # evo_agents.hub.runs.NOTICE_KINDS
ADMIN_PAGE = "/admin/plugins"
MAX_REASON_CHARS = 2000  # plugins.disabled_reason (0034)
PLUGIN_NAME = PLUGIN_ID  # a manifest's id
VERSION = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]{1,40})?$"  # compat.PLUGIN_VERSION
UNAVAILABLE = "the blob store did not answer; nothing changed, try again shortly"
IMAGE = "image"  # plugin_versions.signature_kind of a core plugin that comes with the hub's image (0038)
FROM_IMAGE = (
    "plugin {name} is a core plugin of the hub's image: it is installed, upgraded and removed with the image, not "
    "through the store; nothing changed"
)


class PluginVersion(BaseModel):
    version: str
    sha256: str
    size: int
    publisher: str
    tier: Literal[TIERS]
    plugin_api: int
    engines: str = Field(description="the evo-agents versions it runs on, as its manifest gives them")
    permissions: list[str]
    signature_kind: Literal[(*SIGNATURE_KINDS, IMAGE)] = Field(
        description="image: a core plugin that comes with the hub's image, which nobody published or signed here"
    )
    signed_by: str = Field(
        description="the workflow identity of the Sigstore certificate, the Ed25519 key's id, or image:<entry point>"
    )
    compatible: bool = Field(description="whether it runs on this hub's release")
    incompatible_reason: str | None
    published_by: str | None = Field(description="null for a core plugin of the hub's image")
    published_at: datetime


class Plugin(BaseModel):
    name: str
    title: str = Field(description="the name its manifest gives it (the installed version's, else the latest's)")
    description: str
    tier: Literal[TIERS]
    publisher: str
    state: Literal[STATES] = Field(description="available: none installed; disabled: installed, turned off")
    installed_version: str | None
    installed_by: str | None
    installed_at: datetime | None
    disabled_reason: str | None
    disabled_at: datetime | None
    latest_version: str
    upgrade_to: str | None = Field(description="the newest version above the installed one that runs on this hub")
    created_at: datetime


class PluginDetail(Plugin):
    manifest: dict = Field(description="the manifest of the installed version, or of the latest one")
    versions: list[PluginVersion] = Field(description="the newest first")


class PluginPublishIn(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="the bundle, committed to the blob store already")
    size: int = Field(ge=1, le=MAX_BUNDLE, description="bytes of the bundle")
    signature: dict | None = Field(
        description="the signature envelope beside the bundle (evo_agents.plugins.signing); null is refused, unsigned"
    )

    @model_validator(mode="after")
    def _bounded(self):
        if self.signature is not None and len(json.dumps(self.signature)) > MAX_SIGNATURE:
            raise ValueError(f"a signature is at most {MAX_SIGNATURE // 1024} KiB")
        return self


class PluginInstallIn(BaseModel):
    version: str | None = Field(None, pattern=VERSION, description="default: the newest that runs on this hub")


class PluginPublished(BaseModel):
    name: str
    created: bool = Field(description="false when the store holds this bundle as this version already")
    version: PluginVersion


@dataclass(frozen=True)
class Checked:
    """A bundle that passed every check, and who signed it."""

    manifest: Manifest
    signed: signing.Verified


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
    and did not remove (``plugin_publishers``, 0149)."""
    p = tables.plugin_publishers.c
    query = select(p.publisher, p.signer).where(p.removed_at.is_(None)).order_by(p.id)
    extra = [(row.publisher, row.signer) for row in await conn.execute(query)]
    return merge_publishers(config.plugin_publishers, extra)


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


def _versions_query():
    v, u = tables.plugin_versions, tables.users
    return select(
        v.c.plugin_id,
        v.c.version,
        v.c.sha256,
        v.c.size,
        v.c.publisher,
        v.c.tier,
        v.c.manifest,
        v.c.signature,
        v.c.signature_kind,
        v.c.signed_by,
        u.c.login.label("published_by"),
        v.c.published_at,
    ).outerjoin_from(v, u, u.c.id == v.c.published_by)


def _plugins_query():
    p, u = tables.plugins, tables.users
    return (
        select(
            p.c.id,
            p.c.name,
            p.c.publisher,
            p.c.installed_version,
            u.c.login.label("installed_by"),
            p.c.installed_at,
            p.c.disabled_reason,
            p.c.disabled_at,
            p.c.created_at,
        )
        .outerjoin_from(p, u, u.c.id == p.c.installed_by)
        .order_by(p.c.name)
    )


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


def newest_first(rows: list) -> list:
    return sorted(rows, key=lambda row: compat.plugin_version_key(row.version), reverse=True)


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
        "created_at": row.created_at,
    }


async def list_plugins(conn: AsyncConnection, hub_version: str) -> list[Plugin]:
    """The plugins of the store; the features of the hub (``plugins.builtin``: canvas, srs, tasks) are none of them."""
    rows = (await conn.execute(_plugins_query().where(tables.plugins.c.builtin.is_(False)))).all()
    by_plugin: dict[int, list] = {}
    for version in (await conn.execute(_versions_query())).all():
        by_plugin.setdefault(version.plugin_id, []).append(version)
    found = []
    for row in rows:
        held = newest_first(by_plugin.get(row.id, []))
        if held:
            versions = [version_model(v, hub_version) for v in held]
            found.append(Plugin(**plugin_model(row, versions, shown_version(row, held).manifest)))
    return found


async def find(conn: AsyncConnection, name: str, *, lock: bool = False, builtin: bool = False):
    """The row of plugin ``name`` from the plugins query; None when the hub has no such plugin, or when ``name`` is a
    feature of the hub (``plugins.builtin``) and ``builtin`` is False: the store shows, installs and removes none of
    them."""
    query = _plugins_query().where(tables.plugins.c.name == name)
    if not builtin:
        query = query.where(tables.plugins.c.builtin.is_(False))
    if lock:
        query = query.with_for_update(of=tables.plugins)
    return (await conn.execute(query)).first()


async def version_rows(conn: AsyncConnection, plugin_id: int) -> list:
    """Every version of the plugin, newest first."""
    query = _versions_query().where(tables.plugin_versions.c.plugin_id == plugin_id)
    return newest_first((await conn.execute(query)).all())


def not_found(name: str) -> HTTPException:
    return HTTPException(404, f"no plugin {name} on this hub: see `evo-agents hub plugin list`")


async def detail(conn: AsyncConnection, name: str, hub_version: str) -> PluginDetail:
    row = await find(conn, name)
    rows = [] if row is None else await version_rows(conn, row.id)
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
    blobs = tables.blobs
    held = select(blobs.c.size).where(blobs.c.project_id.is_(None), blobs.c.sha256 == sha256)
    row = (await conn.execute(held)).first()
    if row is None:
        raise HTTPException(
            422,
            f"bundle {sha256} is not committed for the hub: ask for an upload without a project (POST "
            "/v1/blobs/uploads, kind plugin-bundle), PUT it and commit it first; nothing was published",
        )
    if row.size != size:
        raise HTTPException(
            422, f"bundle {sha256} has {row.size} bytes, not the {size} declared; nothing was published"
        )


async def held_version(conn: AsyncConnection, name: str, sha256: str):
    """The version of plugin ``name`` whose bundle is ``sha256``, or None."""
    row = await find(conn, name)
    if row is None:
        return None
    return next((v for v in await version_rows(conn, row.id) if v.sha256 == sha256), None)


async def record_version(conn: AsyncConnection, user: Principal, body: PluginPublishIn, checked: Checked) -> bool:
    """Write the version ``checked`` describes, creating its plugin; False when it is there already. 409 for a
    plugin of another publisher, or a version number published with other bytes."""
    manifest, plugins = checked.manifest, tables.plugins
    await conn.execute(
        pg_insert(plugins).values(name=manifest.id, publisher=manifest.publisher).on_conflict_do_nothing()
    )
    row = await find(conn, manifest.id, lock=True, builtin=True)  # versions of one plugin are written one at a time
    versions = await version_rows(conn, row.id)
    if from_image(versions):
        raise HTTPException(409, FROM_IMAGE.format(name=manifest.id))
    if row.publisher != manifest.publisher:
        raise HTTPException(
            409,
            f"plugin {manifest.id} is published by {row.publisher}; publisher {manifest.publisher} cannot publish a "
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
    await conn.execute(
        insert(tables.plugin_versions).values(
            plugin_id=row.id,
            version=manifest.version,
            sha256=body.sha256,
            size=body.size,
            r2_key=BLOB_PREFIX + body.sha256,
            publisher=manifest.publisher,
            tier=manifest.tier,
            manifest=manifest.data,
            signature=body.signature,
            signature_kind=checked.signed.kind,
            signed_by=checked.signed.signed_by,
            published_by=user.user_id,
        )
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
    if upgrade and target is not None and compat.plugin_version_key(target) <= compat.plugin_version_key(installed):
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


async def set_installed(conn: AsyncConnection, user: Principal, row, version: str, *, upgrade: bool) -> None:
    """Install ``version`` of plugin ``row``, turned on, with its audit row."""
    plugins = tables.plugins
    await conn.execute(
        update(plugins)
        .where(plugins.c.id == row.id)
        .values(
            installed_version=version,
            installed_by=user.user_id,
            installed_at=func.now(),
            disabled_reason=None,
            disabled_at=None,
        )
    )
    action = audit.PLUGIN_UPGRADE if upgrade else audit.PLUGIN_INSTALL
    target = f"plugin:{row.name}@{version}"
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target)


async def set_removed(conn: AsyncConnection, user: Principal, row) -> None:
    plugins = tables.plugins
    cleared = {"installed_version": None, "installed_by": None, "installed_at": None}
    await conn.execute(
        update(plugins).where(plugins.c.id == row.id).values(**cleared, disabled_reason=None, disabled_at=None)
    )
    await revoke_tokens(conn, row.id)  # its backend's token stops working now, not when the host next pulls
    target = f"plugin:{row.name}@{row.installed_version}"
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.PLUGIN_REMOVE, target=target)


# At every start


def _installed_query():
    p, v = tables.plugins, tables.plugin_versions
    return (
        select(p.c.id, p.c.name, p.c.installed_version, v.c.manifest)
        .join_from(p, v, (v.c.plugin_id == p.c.id) & (v.c.version == p.c.installed_version))
        .where(p.c.disabled_reason.is_(None), v.c.signature_kind != IMAGE)  # the image's run on its release
        .order_by(p.c.name)
        .with_for_update(of=p)
    )


async def hub_admin_ids(conn: AsyncConnection, admins: frozenset[str]) -> list[int]:
    """The ids of the hub admins (EVO_HUB_ADMINS) who have signed in, by id."""
    users = tables.users
    logins = literal(sorted(admins), ARRAY(Text))
    query = select(users.c.id).where(func.lower(users.c.login) == any_(logins)).order_by(users.c.id)
    return list((await conn.execute(query)).scalars())


async def _turn_off(conn: AsyncConnection, row, reason: str, admin_ids: list[int], hub_version: str) -> None:
    plugins = tables.plugins
    await conn.execute(
        update(plugins)
        .where(plugins.c.id == row.id)
        .values(disabled_reason=reason[:MAX_REASON_CHARS], disabled_at=func.now())
    )
    await revoke_tokens(conn, row.id)
    target = f"plugin:{row.name}@{row.installed_version}"
    await audit.record(conn, actor_id=None, token_id=None, action=audit.PLUGIN_DISABLE, target=target)
    body = (
        f"{reason}. The hub turned it off and serves on without it. Upgrade it to a version that runs on "
        f"{hub_version} with `evo-agents hub plugin upgrade {row.name}`, or remove it."
    )
    details = {"plugin": row.name, "version": row.installed_version, "hub_version": hub_version}
    for user_id in admin_ids:
        await notify(
            conn,
            user_id=user_id,
            kind="notice",
            notice_kind=NOTICE,
            title=f"Plugin {row.name} turned off: it does not run on evo-agents {hub_version}",
            body=body,
            details=details,
            link=ADMIN_PAGE,
        )


def _image_version(manifest: Manifest, value: str) -> dict:
    """The version row of core plugin ``manifest`` of the image, from entry point ``value``: its manifest hashed as
    canonical JSON stands for the bundle it does not have."""
    canonical = json.dumps(manifest.data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return {
        "version": manifest.version,
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "size": len(canonical),
        "r2_key": f"{IMAGE}:{value}",
        "publisher": manifest.publisher,
        "tier": manifest.tier,
        "manifest": manifest.data,
        "signature": {},
        "signature_kind": IMAGE,
        "signed_by": f"{IMAGE}:{value}"[:200],
        "published_by": None,
    }


async def _missing_tables(conn: AsyncConnection, manifest: Manifest) -> list[str]:
    found = []
    for name in manifest.tables():
        if (await conn.execute(select(func.to_regclass(literal(f"public.{name}", Text))))).scalar() is None:
            found.append(name)
    return found


async def _record_image_plugin(conn: AsyncConnection, plugin) -> bool:
    """Record core plugin ``plugin`` (a ``CorePlugin``) as installed at its version; False, logged, when the store
    holds a plugin of that name that is not the image's, or the hub's schema lacks a table it owns."""
    manifest, plugins, versions = plugin.manifest, tables.plugins, tables.plugin_versions
    missing = await _missing_tables(conn, manifest)
    if missing:
        log.error(
            "core plugin not recorded: the hub's migrations do not create its tables",
            extra={"plugin": manifest.id, "tables": missing},
        )
        return False
    builtin = bool(getattr(plugin, "builtin", False))  # a feature of the hub (plugin_calls.BUILTIN)
    await conn.execute(
        pg_insert(plugins)
        .values(name=manifest.id, publisher=manifest.publisher, builtin=builtin)
        .on_conflict_do_nothing()
    )
    await conn.execute(update(plugins).where(plugins.c.name == manifest.id).values(builtin=builtin))
    row = await find(conn, manifest.id, lock=True, builtin=True)
    held = await version_rows(conn, row.id)
    if row.publisher != manifest.publisher or any(version.signature_kind != IMAGE for version in held):
        log.error("core plugin not recorded: the store holds a plugin of its name", extra={"plugin": manifest.id})
        return False
    values = {"plugin_id": row.id, **_image_version(manifest, plugin.value)}
    changed = {key: values[key] for key in ("sha256", "size", "r2_key", "manifest", "signed_by")}
    upsert = pg_insert(versions).values(values)
    await conn.execute(
        upsert.on_conflict_do_update(index_elements=[versions.c.plugin_id, versions.c.version], set_=changed)
    )
    if row.installed_version != manifest.version or row.disabled_reason is not None:
        on = {"installed_version": manifest.version, "installed_by": None, "installed_at": func.now()}
        await conn.execute(
            update(plugins).where(plugins.c.id == row.id).values({**on, "disabled_reason": None, "disabled_at": None})
        )
        if manifest.per_project() and not builtin:  # hub-wide, or a feature of the hub: nobody installed anything
            target = f"plugin:{manifest.id}@{manifest.version} via={IMAGE}"
            await audit.record(conn, actor_id=None, token_id=None, action=audit.PLUGIN_INSTALL, target=target)
    return True


async def register_core_plugins(engine: AsyncEngine, found) -> list[str]:
    """Record in the store each core plugin of the image (``found``, of ``evo_agents.plugins.core``), installed at its
    version. One that projects enable (``Manifest.per_project``) they then enable, grant what it asks for and configure
    as any other; one that runs hub-wide (Telegram) is listed in the store only, and no project enables it. A plugin of
    the image that left it is no longer installed; its rows, its projects' grants and configs, and the data in its
    tables stay. The names recorded."""
    recorded, kept = [], set()
    async with engine.begin() as conn:
        for plugin in found:
            if await _record_image_plugin(conn, plugin):
                recorded.append(plugin.manifest.id)
                kept.add(plugin.manifest.id)
        p, v = tables.plugins, tables.plugin_versions
        left = (
            select(p.c.id, p.c.name, p.c.installed_version, v.c.manifest)
            .join_from(p, v, (v.c.plugin_id == p.c.id) & (v.c.version == p.c.installed_version))
            .where(v.c.signature_kind == IMAGE, p.c.name.not_in(kept or [""]))
            .with_for_update(of=p)
        )
        for row in (await conn.execute(left)).all():
            cleared = {"installed_version": None, "installed_by": None, "installed_at": None}
            await conn.execute(
                update(p).where(p.c.id == row.id).values({**cleared, "disabled_reason": None, "disabled_at": None})
            )
            if is_per_project(row.manifest):
                target = f"plugin:{row.name}@{row.installed_version} via={IMAGE}"
                await audit.record(conn, actor_id=None, token_id=None, action=audit.PLUGIN_REMOVE, target=target)
            log.warning("core plugin left the image: no longer installed, its data kept", extra={"plugin": row.name})
    if recorded:
        log.info("core plugins of the image recorded", extra={"plugins": recorded})
    return recorded


async def disable_incompatible(engine: AsyncEngine, admins: frozenset[str], hub_version: str) -> list[str]:
    """Turn off every installed plugin whose version does not run on ``hub_version``, telling the hub admins; the
    names turned off."""
    turned_off = []
    async with engine.begin() as conn:
        rows = (await conn.execute(_installed_query())).all()
        admin_ids = None
        for row in rows:
            manifest = row.manifest
            problem = compat.problem(
                manifest["engines"], manifest["plugin_api"], hub_version, manifest.get("contributes")
            )
            if problem is None:
                continue
            if admin_ids is None:
                admin_ids = await hub_admin_ids(conn, admins)
            reason = f"plugin {row.name} {row.installed_version} does not run on evo-agents {hub_version}: {problem}"
            await _turn_off(conn, row, reason, admin_ids, hub_version)
            turned_off.append(row.name)
    for name in turned_off:
        log.warning("plugin turned off: it does not run on this release", extra={"plugin": name})
    return turned_off
