"""The SQL of restoring a failed run's cause (``evo_agents.hub.server.run_cause``): the run's cause, locked, and the
newest of its cause events."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.runs.models.run_cause import CAUSE_KEY, CLASSIFIED, RESTORED


async def newest_cause_event(conn: AsyncConnection, run_id: int):
    """(seq, body) of the newest ``system`` event of run ``run_id`` that classified its cause or restored it, or
    None."""
    e = tables.run_events
    step = e.c.body[CAUSE_KEY]["step"].astext
    query = (
        select(e.c.seq, e.c.body)
        .where(e.c.run_id == run_id, e.c.kind == "system", step.in_((CLASSIFIED, RESTORED)))
        .order_by(e.c.seq.desc())
        .limit(1)
    )
    return (await conn.execute(query)).one_or_none()


async def locked_cause(conn: AsyncConnection, project_id: int, run_id: int):
    """(failure_cause,) of run ``run_id`` of project ``project_id``, its row locked, or None."""
    r = tables.runs
    query = select(r.c.failure_cause).where(r.c.id == run_id, r.c.project_id == project_id).with_for_update()
    return (await conn.execute(query)).one_or_none()


async def set_cause(conn: AsyncConnection, run_id: int, cause: str | None) -> None:
    r = tables.runs
    await conn.execute(r.update().values(failure_cause=cause).where(r.c.id == run_id))
