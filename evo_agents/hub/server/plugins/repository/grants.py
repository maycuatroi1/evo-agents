"""The queries of the plugins in projects (table project_plugins, schema 0035): which installed plugin a project
enabled, and what it granted."""

from __future__ import annotations

from sqlalchemy import delete, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import effective_grant, enabled_query, enablements


def project_query(project_id: int):
    """The installed plugins, by name, with what project ``project_id`` enabled and granted."""
    p, v, pp, e = tables.plugins, tables.plugin_versions, tables.project_plugins, enablements()
    enabler, granter = tables.users.alias("enabler"), tables.users.alias("granter")
    return (
        select(
            p.c.id,
            p.c.name,
            p.c.publisher,
            p.c.installed_version,
            p.c.disabled_reason,
            p.c.builtin,
            v.c.manifest,
            v.c.sha256,
            e.c.plugin_id.label("enabled_id"),
            e.c.permissions,
            enabler.c.login.label("enabled_by"),
            pp.c.enabled_at,
            granter.c.login.label("granted_by"),
            pp.c.granted_at,
            pp.c.records_level,
        )
        .join_from(p, v, (v.c.plugin_id == p.c.id) & (v.c.version == p.c.installed_version))
        .where(or_(p.c.project_id.is_(None), p.c.project_id == project_id))  # one installed for another is not seen
        .outerjoin(e, (e.c.plugin_id == p.c.id) & (e.c.project_id == project_id))
        .outerjoin(pp, (pp.c.plugin_id == p.c.id) & (pp.c.project_id == project_id))
        .outerjoin(enabler, enabler.c.id == pp.c.enabled_by)
        .outerjoin(granter, granter.c.id == pp.c.granted_by)
        .order_by(p.c.name)
    )


async def project_rows(conn: AsyncConnection, project_id: int) -> list:
    return list(await conn.execute(project_query(project_id)))


async def installed_in_project(conn: AsyncConnection, project_id: int, name: str, *, lock: bool = False):
    """The row of the installed plugin ``name`` from ``project_query``, or None."""
    query = project_query(project_id).where(tables.plugins.c.name == name)
    if lock:  # enabling waits for an install, upgrade or removal under way, and they for it
        query = query.with_for_update(of=tables.plugins)
    return (await conn.execute(query)).first()


async def add_enablement(conn: AsyncConnection, values: dict) -> None:
    await conn.execute(insert(tables.project_plugins).values(values))


async def set_grant(conn: AsyncConnection, project_id: int, plugin_id: int, grant: dict) -> None:
    pp = tables.project_plugins
    mine = (pp.c.project_id == project_id) & (pp.c.plugin_id == plugin_id)
    await conn.execute(update(pp).where(mine).values(grant))


async def delete_enablement(conn: AsyncConnection, project_id: int, plugin_id: int) -> None:
    pp = tables.project_plugins
    await conn.execute(delete(pp).where((pp.c.project_id == project_id) & (pp.c.plugin_id == plugin_id)))


async def enabled_rows_of(conn: AsyncConnection, name: str) -> list:
    """The rows of ``plugin_calls.enabled_query`` of plugin ``name``, one for each project that enabled it, by
    project name."""
    query = enabled_query().where(tables.plugins.c.name == name).order_by(tables.projects.c.name)
    return list(await conn.execute(query))


async def project_grants(conn: AsyncConnection, name: str) -> dict[str, tuple[str, ...]]:
    """project name -> what plugin ``name`` may do there, for each project that enabled it; empty when it is not
    installed or turned off."""
    return {row.project: effective_grant(row) for row in await enabled_rows_of(conn, name)}
