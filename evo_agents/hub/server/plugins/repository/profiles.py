"""The query of the agent profiles of a project whose layer of a plugin's config is read or written."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def profile_row(conn: AsyncConnection, project_id: int, handle: str | None):
    """(id, handle, deleted_at) of agent profile ``handle`` of the project, the default one for None; None when it has
    none."""
    ap = tables.agent_profiles
    query = select(ap.c.id, ap.c.handle, ap.c.deleted_at).where(ap.c.project_id == project_id)
    query = query.where(ap.c.is_default) if handle is None else query.where(ap.c.handle == handle)
    return (await conn.execute(query)).first()
