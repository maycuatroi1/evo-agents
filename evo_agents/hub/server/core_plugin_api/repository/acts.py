"""The rows the acts of a member through a core plugin read: a grant held, and the agents a member may talk to."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def holds_grant(conn: AsyncConnection, user_id: int, project_id: int) -> bool:
    """Whether user ``user_id`` holds a grant on project ``project_id``."""
    g = tables.grants
    query = select(g.c.role).where(g.c.user_id == user_id, g.c.project_id == project_id)
    return (await conn.execute(query)).first() is not None


async def agents_of(conn: AsyncConnection, user_id: int, roles: Sequence[str]):
    """The agents not deleted of the projects where user ``user_id`` holds one of ``roles``, by project then the
    default first: rows of handle, name and project."""
    ap, p, g = tables.agent_profiles, tables.projects, tables.grants
    query = (
        select(ap.c.handle, ap.c.name, p.c.name.label("project"))
        .join_from(ap, p, p.c.id == ap.c.project_id)
        .join(g, and_(g.c.project_id == ap.c.project_id, g.c.user_id == user_id))
        .where(ap.c.deleted_at.is_(None), g.c.role.in_(list(roles)))
        .order_by(p.c.name, ap.c.is_default.desc(), ap.c.handle)
    )
    return (await conn.execute(query)).all()
