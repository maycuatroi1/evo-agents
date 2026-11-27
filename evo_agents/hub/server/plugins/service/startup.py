"""What the hub's store of plugins does as the api starts.

A core plugin that comes with the hub's image (``register_core_plugins``, as the api starts) is recorded as a version
of signature kind ``image``, published and installed by nobody, which the store neither installs, upgrades nor removes
(409): the image does. It is recorded only when the hub's schema has every table it owns. One that projects enable
(``Manifest.per_project``) is then enabled per project as any other, and its install is audited; one that runs hub-wide
(Telegram) is listed here and nowhere in a project (``service.grants``), with no audit row: it ran
before the store listed it, and nobody did anything.

``disable_incompatible`` runs as the api starts: an installed plugin whose version no longer runs on this release
(the hub was upgraded past its ``engines``) is turned off with the reason, an audit row and a notice
``plugin_disabled`` to every hub admin who has signed in, and the hub serves on. Installing it again, or upgrading it
to a version that runs, turns it back on. A core plugin of the image is not among them: the image loads it only on a
release it runs on, and records it as it starts.
"""

from __future__ import annotations

import hashlib
import json
import logging

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.server import audit
from evo_agents.hub.server.notifications.service.notifier import NOTIFIER
from evo_agents.hub.server.plugins.models.store import ADMIN_PAGE, IMAGE, NOTICE
from evo_agents.hub.server.plugins.repository import store as repository
from evo_agents.hub.server.plugins.repository import tokens as token_rows
from evo_agents.hub.server.plugins.service.store import hub_admin_ids
from evo_agents.plugins import compat
from evo_agents.plugins.manifest import Manifest, is_per_project

log = logging.getLogger(__name__)


# At every start


async def turn_off(conn: AsyncConnection, row, reason: str, admin_ids: list[int], hub_version: str) -> None:
    await repository.set_disabled(conn, row.id, reason)
    await token_rows.revoke_tokens(conn, row.id)
    target = f"plugin:{row.name}@{row.installed_version}"
    await audit.record(conn, actor_id=None, token_id=None, action=audit.PLUGIN_DISABLE, target=target)
    body = (
        f"{reason}. The hub turned it off and serves on without it. Upgrade it to a version that runs on "
        f"{hub_version} with `evo-agents hub plugin upgrade {row.name}`, or remove it."
    )
    details = {"plugin": row.name, "version": row.installed_version, "hub_version": hub_version}
    for user_id in admin_ids:
        await NOTIFIER.notify(
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


async def _record_image_plugin(conn: AsyncConnection, plugin) -> bool:
    """Record core plugin ``plugin`` (a ``CorePlugin``) as installed at its version; False, logged, when the store
    holds a plugin of that name that is not the image's, or the hub's schema lacks a table it owns."""
    manifest = plugin.manifest
    missing = await repository.missing_tables(conn, manifest.tables())
    if missing:
        log.error(
            "core plugin not recorded: the hub's migrations do not create its tables",
            extra={"plugin": manifest.id, "tables": missing},
        )
        return False
    builtin = bool(getattr(plugin, "builtin", False))  # a feature of the hub (plugin_calls.BUILTIN)
    await repository.add_plugin(conn, manifest.id, manifest.publisher, builtin=builtin)
    await repository.set_builtin(conn, manifest.id, builtin)
    row = await repository.find(conn, manifest.id, lock=True, builtin=True)
    held = await repository.version_rows(conn, row.id)  # pyright: ignore[reportOptionalMemberAccess]
    if row.publisher != manifest.publisher or any(version.signature_kind != IMAGE for version in held):  # pyright: ignore[reportOptionalMemberAccess]
        log.error("core plugin not recorded: the store holds a plugin of its name", extra={"plugin": manifest.id})
        return False
    values = {"plugin_id": row.id, **_image_version(manifest, plugin.value)}  # pyright: ignore[reportOptionalMemberAccess]
    await repository.put_image_version(conn, values)
    if row.installed_version != manifest.version or row.disabled_reason is not None:  # pyright: ignore[reportOptionalMemberAccess]
        await repository.set_installed(conn, row.id, manifest.version, None)  # pyright: ignore[reportOptionalMemberAccess]
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
        for row in await repository.left_image_rows(conn, kept):
            await repository.set_removed(conn, row.id)
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
        rows = await repository.installed_rows(conn)
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
            await turn_off(conn, row, reason, admin_ids, hub_version)
            turned_off.append(row.name)
    for name in turned_off:
        log.warning("plugin turned off: it does not run on this release", extra={"plugin": name})
    return turned_off
