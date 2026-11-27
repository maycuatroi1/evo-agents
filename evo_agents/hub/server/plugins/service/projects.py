"""What the routes of a project's plugins check before they read or write a layer of a plugin's config
(``routes.projects``): the caller's grant, the agent profile a layer belongs to, and that the project enabled the
plugin; and what each of those routes does, in a transaction of its own."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.plugin_calls import enabled_plugins
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.credentials.models.sealing import Sealer
from evo_agents.hub.server.plugins.models.configs import (
    EffectiveConfig,
    Place,
    PluginConfigLayer,
    PluginConfigRollback,
    PluginConfigWrite,
    RunPlugin,
)
from evo_agents.hub.server.plugins.models.grants import ProjectPlugin
from evo_agents.hub.server.plugins.repository.profiles import profile_row
from evo_agents.hub.server.plugins.service.configs import (
    effective,
    layer_versions,
    read_layer,
    rollback_layer,
    run_plugins,
    write_layer,
)
from evo_agents.hub.server.plugins.service.grants import (
    check_admin,
    check_reader,
    disable,
    enable,
    installed_row,
    list_project_plugins,
    project_plugin,
)
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.runs.service.views import readable_run
from evo_agents.hub.server.security import Principal


async def reading(conn: AsyncConnection, user, project: str) -> ProjectAccess:
    access = await project_access(conn, user, project)
    check_reader(access)
    return access


async def profile(conn: AsyncConnection, access: ProjectAccess, handle: str | None, *, live: bool = False):
    """(id, handle) of agent profile ``handle`` of the project, the default one for None; 404 when the project has
    none, or (``live``) only a deleted one."""
    row = await profile_row(conn, access.project_id, handle)
    if row is None or (live and row.deleted_at is not None):
        raise HTTPException(
            404,
            f"project {access.name} has no agent @{handle}: see `evo-agents hub agent list --project {access.name}`",
        )
    return row.id, row.handle


async def enabled(conn: AsyncConnection, access: ProjectAccess, name: str):
    """The row of ``plugin_calls.enabled_query`` of plugin ``name`` in the project; 409 when it is not enabled."""
    await installed_row(conn, access, name)
    found = next((row for row in await enabled_plugins(conn, access.project_id) if row.name == name), None)
    if found is None:
        raise HTTPException(
            409,
            f"plugin {name} is not enabled for project {access.name}, or the hub turned it off: an admin of the "
            f"project enables it with `evo-agents hub plugin enable {name} --project {access.name}`",
        )
    return found


async def place(conn: AsyncConnection, access: ProjectAccess, plugin, handle: str | None, *, live: bool = False):
    if handle is None:
        return Place(plugin.id, plugin.name, "project", access.project_id, access.name)
    profile_id, handle = await profile(conn, access, handle, live=live)
    return Place(plugin.id, plugin.name, "agent", access.project_id, access.name, profile_id, handle)


# Enabling


async def plugins_of(engine: AsyncEngine, user: Principal, project: str, secret: str | None) -> list[ProjectPlugin]:
    """Every plugin installed on the hub, with whether ``project`` enabled it, for a reader of the project, in a
    transaction of its own. ``secret`` signs the tickets of the plugins' pages (``service.pages``)."""
    async with UnitOfWork(engine) as conn:
        return await list_project_plugins(conn, await reading(conn, user, project), secret)


async def plugin_of(engine: AsyncEngine, user: Principal, project: str, name: str, secret: str | None):
    """Installed plugin ``name`` as ``project`` sees it, for a reader of the project, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        return await project_plugin(conn, await reading(conn, user, project), name, secret)


async def enable_in(
    engine: AsyncEngine,
    user: Principal,
    project: str,
    name: str,
    permissions: list[str],
    records_level: str | None,
    secret: str | None,
) -> ProjectPlugin:
    """Enable plugin ``name`` for ``project``, for an admin of the project, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        check_admin(access, f"enabling plugin {name}")
        await enable(conn, user, access, name, permissions, records_level)
        return await project_plugin(conn, access, name, secret)


async def disable_in(
    engine: AsyncEngine, user: Principal, project: str, name: str, secret: str | None
) -> ProjectPlugin:
    """Disable plugin ``name`` for ``project``, for an admin of the project, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        check_admin(access, f"disabling plugin {name}")
        await disable(conn, user, access, name)
        return await project_plugin(conn, access, name, secret)


# Config


async def config_layer(
    engine: AsyncEngine, user: Principal, project: str, name: str, agent: str | None
) -> PluginConfigLayer:
    """The current version of the layer of plugin ``name``'s config of ``project``, or of its agent ``agent``, for a
    reader of the project, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        access = await reading(conn, user, project)
        plugin = await enabled(conn, access, name)
        return await read_layer(conn, await place(conn, access, plugin, agent))


async def config_versions(
    engine: AsyncEngine, user: Principal, project: str, name: str, agent: str | None
) -> list[PluginConfigLayer]:
    """Every version of that layer, the newest first, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        access = await reading(conn, user, project)
        plugin = await enabled(conn, access, name)
        return await layer_versions(conn, await place(conn, access, plugin, agent))


async def write_config(
    engine: AsyncEngine,
    user: Principal,
    project: str,
    name: str,
    agent: str | None,
    body: PluginConfigWrite,
    sealer: Sealer | None,
) -> PluginConfigLayer:
    """Write the layer's next version, for an admin of the project, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        check_admin(access, f"changing the config of plugin {name}")
        await installed_row(conn, access, name, lock=True)  # one write of the plugin's config at a time
        plugin = await enabled(conn, access, name)
        at = await place(conn, access, plugin, agent, live=True)
        return await write_layer(conn, user, at, plugin.manifest.get("config"), body, sealer)


async def rollback_config(
    engine: AsyncEngine, user: Principal, project: str, name: str, agent: str | None, body: PluginConfigRollback
) -> PluginConfigLayer:
    """Copy an older version of the layer as its next version, for an admin of the project, in a transaction of its
    own."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        check_admin(access, f"rolling back the config of plugin {name}")
        await installed_row(conn, access, name, lock=True)
        plugin = await enabled(conn, access, name)
        return await rollback_layer(conn, user, await place(conn, access, plugin, agent, live=True), body)


async def effective_config(
    engine: AsyncEngine, user: Principal, project: str, name: str, agent: str | None
) -> EffectiveConfig:
    """The config plugin ``name`` gets in ``project`` for runs under ``agent``, for a reader of the project, in a
    transaction of its own."""
    async with UnitOfWork(engine) as conn:
        access = await reading(conn, user, project)
        plugin = await enabled(conn, access, name)
        profile_id, handle = await profile(conn, access, agent)
        return await effective(conn, plugin, access.project_id, access.name, profile_id, handle)


# Runs


async def plugins_of_run(
    engine: AsyncEngine, user: Principal, project: str, run_id: int, sink: str | None
) -> list[RunPlugin]:
    """The plugins run ``run_id`` ran with, for a caller who may read the run, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        await readable_run(conn, user, project, run_id, sink)
        return await run_plugins(conn, run_id)
