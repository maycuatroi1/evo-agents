"""The plugin a call names, as one project enabled it (``plugin_calls.enabled_query``)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import plugin_calls, tables


async def enabled_in(conn: AsyncConnection, plugin_id: int, project: str):
    """The row of ``plugin_calls.enabled_query`` of plugin ``plugin_id`` in ``project``, or None."""
    query = plugin_calls.enabled_query().where(tables.plugins.c.id == plugin_id, tables.projects.c.name == project)
    return (await conn.execute(query)).first()


async def enabled_named(conn: AsyncConnection, plugin: str, project: str):
    """The row of ``plugin_calls.enabled_query`` of plugin ``plugin`` (by name) in ``project``, or None."""
    query = plugin_calls.enabled_query().where(tables.plugins.c.name == plugin, tables.projects.c.name == project)
    return (await conn.execute(query)).first()
