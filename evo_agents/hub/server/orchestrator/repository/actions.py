"""The rows the Orchestrator's actions read and write (``service.actions``): an ops thread's batch, a thread's outcome,
and the wait of an escalation that an answer wakes or a reassign moves."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def system_spec(conn: AsyncConnection, system_id: int) -> dict:
    s = tables.agent_systems
    return (await conn.execute(select(s.c.spec).where(s.c.id == system_id))).scalar_one()


async def batch_state(conn: AsyncConnection, thread_id: int) -> str | None:
    """The state of the ops batch of thread ``thread_id``; None for none."""
    b = tables.ops_batches
    return (await conn.execute(select(b.c.state).where(b.c.thread_id == thread_id))).scalar_one_or_none()


async def set_outcome(conn: AsyncConnection, thread_id: int, outcome: str, *, close: bool) -> None:
    t = tables.orchestrator_threads
    closed = func.now() if close else None
    await conn.execute(update(t).values(outcome=outcome, closed_at=closed).where(t.c.id == thread_id))


async def wait(conn: AsyncConnection, wait_id: int):
    """Wait ``wait_id``: its state, project and plan; None for none."""
    d = tables.decisions
    return (await conn.execute(select(d.c.state, d.c.project_id, d.c.plan_id).where(d.c.id == wait_id))).one_or_none()


async def release_wait(conn: AsyncConnection, wait_id: int) -> None:
    """No run is spawned for wait ``wait_id`` any more."""
    r = tables.runs
    await conn.execute(update(r).values(spawned_wait_id=None).where(r.c.spawned_wait_id == wait_id))


async def move_wait(conn: AsyncConnection, wait_id: int, asker_run_id: int, child: int | None) -> None:
    """Wait ``wait_id`` of run ``asker_run_id`` goes with the work to run ``child``, whose end answers it."""
    r, d = tables.runs, tables.decisions
    await release_wait(conn, wait_id)
    moved = {"spawned_by_run_id": asker_run_id, "spawned_wait_id": wait_id}
    await conn.execute(update(r).values(**moved).where(r.c.id == child))
    await conn.execute(update(d).values(awaits_run_id=child).where(d.c.id == wait_id))
