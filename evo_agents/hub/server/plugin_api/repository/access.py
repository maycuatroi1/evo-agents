"""The queries of who a call of the plugin API acts as (``service.access``)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import plugin_calls, tables


async def enabled(conn: AsyncConnection, plugin_id: int, project: str):
    """The row of ``plugin_calls.enabled_query`` of plugin ``plugin_id`` in ``project``, or None."""
    query = plugin_calls.enabled_query().where(tables.plugins.c.id == plugin_id, tables.projects.c.name == project)
    return (await conn.execute(query)).first()


async def granter(conn: AsyncConnection, enabled):
    """(id, login) of the admin who granted the plugin of ``enabled`` its permissions in its project, or None."""
    pp, u = tables.project_plugins, tables.users
    query = (
        select(u.c.id, u.c.login)
        .join_from(pp, u, u.c.id == pp.c.granted_by)
        .where(pp.c.project_id == enabled.project_id, pp.c.plugin_id == enabled.id)
    )
    return (await conn.execute(query)).first()


__all__ = ["enabled", "granter"]
