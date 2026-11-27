"""What the routes of a project's plugins check before they read or write a layer of a plugin's config
(``routes.projects``): the caller's grant, the agent profile a layer belongs to, and that the project enabled the
plugin."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.plugin_calls import enabled_plugins
from evo_agents.hub.server.plugins.models.configs import Place
from evo_agents.hub.server.plugins.repository.profiles import profile_row
from evo_agents.hub.server.plugins.service.grants import check_reader, installed_row
from evo_agents.hub.server.projects import ProjectAccess, project_access


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
