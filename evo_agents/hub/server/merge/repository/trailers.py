"""The query of the trailers of a run (``service.trailers``): the login of its principal and the handle of its agent
profile."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def names(conn: AsyncConnection, run_id: int):
    """(login, handle) of run ``run_id``, each None when it has none; None when the run is gone."""
    r, u, ap = tables.runs, tables.users, tables.agent_profiles
    query = (
        select(u.c.login, ap.c.handle)
        .select_from(r.outerjoin(u, u.c.id == r.c.principal_id).outerjoin(ap, ap.c.id == r.c.profile_id))
        .where(r.c.id == run_id)
    )
    return (await conn.execute(query)).one_or_none()
