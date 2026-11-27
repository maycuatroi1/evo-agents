"""What the lease of a run's browser identity reads of the run: its worker, state, dispatcher and their role, project
and identities."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def run_of(conn: AsyncConnection, run_id: int):
    """Run ``run_id`` with the name of its project and the role its dispatcher holds on it; None for none."""
    r, p, g = tables.runs, tables.projects, tables.grants
    query = (
        select(
            r.c.worker_id,
            r.c.state,
            r.c.dispatched_by,
            r.c.project_id,
            r.c.browser_identities,
            r.c.kind,
            p.c.name,
            g.c.role,
        )
        .join(p, p.c.id == r.c.project_id)
        .outerjoin(g, (g.c.user_id == r.c.dispatched_by) & (g.c.project_id == r.c.project_id))
        .where(r.c.id == run_id)
    )
    return (await conn.execute(query)).one_or_none()
