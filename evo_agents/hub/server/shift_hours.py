"""When the night shift queues what (``evo_agents.hub.server.curator``, which imports these back). Inside its window it
queues any of its runs. Outside the window it still moves the Curator's changes on, at any hour: the judge run of a
change waiting for one, its replay runs and the Builder of a planned change, within the same caps, counted toward the
night that last opened (``curator.night_of``). The night's own runs (its review and history runs and the runs of the
charter's night_plans) wait for the window, and one of them still queued when the window ends is cancelled; a run of
a change (a run on a Curator's plan, ``judge.CURATOR_PLAN_PREFIX``) is not. Paused, every queued run of the schedule is
cancelled.
"""

from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import judge, replay, tables
from evo_agents.hub.server.run_state import move_run

WAITING_STATES = ("planned", *replay.REPLAY_STATES)  # a change in one of these may want a run outside the window


async def cancel_queued(
    conn: AsyncConnection, schedule_ids: list[int], reason: str, *, changes_too: bool = True
) -> list[int]:
    """Cancel the runs of ``schedule_ids`` still queued, but a run that resumes a parked one, which its owner's answer
    queued, and, without ``changes_too``, a run of a Curator's change; their ids."""
    r = tables.runs
    query = (
        select(r.c.id)
        .where(r.c.schedule_id.in_(schedule_ids), r.c.state == "queued", r.c.resume_of_run_id.is_(None))
        .order_by(r.c.id)
        .with_for_update(skip_locked=True)
    )
    if not changes_too:
        query = query.where(r.c.plan_id.is_(None) | ~r.c.plan_id.startswith(judge.CURATOR_PLAN_PREFIX))
    cancelled = []
    for run_id in (await conn.execute(query)).scalars().all():
        await move_run(conn, run_id, "queued", "cancelled", "owner", reason=reason)
        cancelled.append(run_id)
    return cancelled


async def changes_waiting(conn: AsyncConnection, project_id: int) -> bool:
    """Whether a change of the Curator in the project may want a run now: planned (its Builder), or in a state its
    judge or replay runs come in."""
    c = tables.curator_changes
    query = select(exists().where(c.c.project_id == project_id, c.c.state.in_(WAITING_STATES)))
    return bool((await conn.execute(query)).scalar_one())
