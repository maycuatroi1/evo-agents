"""The SQL of a self-managed project (``evo_agents.hub.server.project_admin``): its grants, the grant one login holds,
and its self-managed state."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def grants_of(conn: AsyncConnection, project_id: int):
    """(login, role, max_level, granted_by, granted_at) of every grant on project ``project_id``, by login."""
    g, u, by = tables.grants, tables.users, tables.users.alias("granter")
    return await conn.execute(
        select(u.c.login, g.c.role, g.c.max_level, by.c.login.label("granted_by"), g.c.granted_at)
        .join_from(g, u, u.c.id == g.c.user_id)
        .outerjoin(by, by.c.id == g.c.granted_by)
        .where(g.c.project_id == project_id)
        .order_by(u.c.login)
    )


async def set_self_managed(conn: AsyncConnection, project_id: int, enabled: bool, owners: list[str]) -> None:
    p = tables.projects
    await conn.execute(update(p).where(p.c.id == project_id).values(self_managed=enabled, trusted_owners=owners))


async def held_grant(conn: AsyncConnection, project_id: int, login: str):
    """(role, max_level) of the grant ``login`` (matched without case) holds on project ``project_id``, or None."""
    g, u = tables.grants, tables.users
    found = await conn.execute(
        select(g.c.role, g.c.max_level)
        .join_from(g, u, u.c.id == g.c.user_id)
        .where(
            g.c.project_id == project_id,
            u.c.login.ilike(login.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")),
        )
    )
    return found.first()
