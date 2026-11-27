"""Every query of what the agent of a triage thread does on its own (``triage.service.actions`` is the design): the
thread of a chat run, its failed run, and the writes of a rerun and a cancel."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def thread_at_root(conn: AsyncConnection, chat_id: int, root_id: int):
    """(id, run_id, cause, cause_inferred, auto_reruns) of the thread of message ``root_id`` of chat ``chat_id``, its
    row locked; None for none."""
    t = tables.run_triage
    return (
        await conn.execute(
            select(t.c.id, t.c.run_id, t.c.cause, t.c.cause_inferred, t.c.auto_reruns)
            .where(t.c.chat_id == chat_id, t.c.root_message_id == root_id)
            .with_for_update()
        )
    ).one_or_none()


async def failed_row(conn: AsyncConnection, run_id: int):
    """What a triage call reads of the run ``run_id`` of a thread, with its project's name and triage switch."""
    r, p = tables.runs, tables.projects
    return (
        await conn.execute(
            select(
                r.c.id,
                r.c.state,
                r.c.kind,
                r.c.project_id,
                r.c.plan_id,
                r.c.step_key,
                r.c.repo,
                r.c.repos,
                r.c.worker_id,
                r.c.runtime,
                r.c.failure_cause,
                r.c.dispatched_by,
                r.c.dispatched_via,
                p.c.name.label("project"),
                p.c.triage_enabled,
            )
            .join_from(r, p, p.c.id == r.c.project_id)
            .where(r.c.id == run_id)
        )
    ).one()


async def worker_of(conn: AsyncConnection, run_id: int) -> int | None:
    r = tables.runs
    return (await conn.execute(select(r.c.worker_id).where(r.c.id == run_id))).scalar_one()


async def count_rerun(conn: AsyncConnection, triage_id: int) -> None:
    """Count one rerun by the agent of thread ``triage_id``."""
    t = tables.run_triage
    await conn.execute(update(t).where(t.c.id == triage_id).values(auto_reruns=t.c.auto_reruns + 1))


async def ask_cancel(conn: AsyncConnection, run_id: int) -> bool:
    """Ask the worker of run ``run_id`` to stop it; False when it was asked already."""
    r = tables.runs
    ask = (
        update(r)
        .values(cancel_requested_at=func.now())
        .where(r.c.id == run_id, r.c.cancel_requested_at.is_(None))
        .returning(r.c.id)
    )
    return (await conn.execute(ask)).one_or_none() is not None
