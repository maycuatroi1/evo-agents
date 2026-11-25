"""Stalled plans and chat runs queued too long (plan orchestrator-keeps-plans-moving): what the pass of the ops queue
(``server.ops_queue.ops_pass``, in the reaper's pass every minute) writes as operations events, for a project whose
default system turns ops on, in that project's transaction, after the queue read its events. It calls no model.

``plan.stalled``: an active plan a system ran (a trace names it, ``system_traces.plan_id``) that has a ready step
(pending or in_progress, every step of its depends_on done: ``runs.steps_to_go``), no plan run or step run active,
no trace of it with a run queued, held, in review or parked, and neither a new revision nor a move of one of its traces
in ``ops.plan_stall_minutes`` of the default system. A plan the Curator made (its night shift runs it) never stalls
here. Each stall writes once: the event names the plan's revision and its newest run (of its own or of its traces),
and a plan with the same two already written is that stall again; a new revision or a new run makes the next one.

``run.queued_long``: a chat run still queued ``ops.plan_stall_minutes`` after it was queued, once a run, unless it is pinned to a worker of ``ops.quiet_workers`` (a run with no pin is not skipped).

The pass of a project takes a lock of its own (``LOCK``, tried, not waited on), so two hubs never write one stall
twice. SQLAlchemy Core on ``evo_agents.hub.tables``; nothing here imports ``evo_agents.hub.server``.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import hub_events, ops_events, ops_queue, runs, system_context, tables

LOCK = "evo-ops-stalls:{project_id}"  # the text whose hash names the lock of a project's pass
OWN_KINDS = ("plan", "step")  # the runs of a plan of its own; a trace's chat runs are the others
MAX_QUEUED = 100  # chat runs one pass writes run.queued_long for


async def check(conn: AsyncConnection, project_id: int) -> dict[str, int]:
    """Write ``plan.stalled`` for each stalled plan of the project and ``run.queued_long`` for each chat run queued
    too long, in the caller's transaction, when its default system turns ops on; how many of each."""
    found = {"stalled": 0, "queued_long": 0}
    config = await ops_queue.ops_config(conn, project_id)
    if not config.enabled:
        return found
    key = func.hashtextextended(LOCK.format(project_id=project_id), 0)
    if not await conn.scalar(select(func.pg_try_advisory_xact_lock(key))):
        return found
    cutoff = func.now() - timedelta(minutes=int(config.plan_stall_minutes))
    found["stalled"] = await _stalled(conn, project_id, cutoff)
    found["queued_long"] = await _queued_long(conn, project_id, cutoff, config.quiet_workers)
    return found


def _trace_runs(project_id: int, plan_id: str):
    """The ids of the runs of the traces of plan ``plan_id``."""
    t, s = tables.system_traces, tables.system_spans
    return (
        select(s.c.run_id)
        .join_from(s, t, t.c.id == s.c.trace_id)
        .where(t.c.project_id == project_id, t.c.plan_id == plan_id, s.c.name == system_context.INVOKE)
    )


def _candidates(project_id: int, cutoff):
    """The query of the project's active plans that a system ran, none of whose runs is active, and that neither a
    revision nor a trace moved since ``cutoff``: plan_id, revision, body."""
    p, t, s, r, c = tables.plans, tables.system_traces, tables.system_spans, tables.runs, tables.curator_changes
    traced = and_(t.c.project_id == project_id, t.c.plan_id == p.c.plan_id)
    active = r.c.state.in_(runs.ACTIVE_STATES)
    own_active = exists().where(
        r.c.project_id == project_id, r.c.plan_id == p.c.plan_id, r.c.kind.in_(OWN_KINDS), active
    )
    trace_active = exists().where(
        traced, s.c.trace_id == t.c.id, s.c.name == system_context.INVOKE, r.c.id == s.c.run_id, active
    )
    return select(p.c.plan_id, p.c.revision, p.c.body).where(
        p.c.project_id == project_id,
        p.c.area == "active",
        p.c.updated_at < cutoff,
        exists().where(traced),
        ~exists().where(traced, t.c.last_at >= cutoff),
        ~exists().where(c.c.project_id == project_id, c.c.plan_id == p.c.plan_id),
        ~own_active,
        ~trace_active,
    )


async def _newest_run(conn: AsyncConnection, project_id: int, plan_id: str) -> int | None:
    """The newest run of plan ``plan_id``: a plan run or step run of it, or a run of one of its traces."""
    r = tables.runs
    own = and_(r.c.project_id == project_id, r.c.plan_id == plan_id, r.c.kind.in_(OWN_KINDS))
    query = select(func.max(r.c.id)).where(or_(own, r.c.id.in_(_trace_runs(project_id, plan_id))))
    return await conn.scalar(query)


async def _stalled(conn: AsyncConnection, project_id: int, cutoff) -> int:
    e = tables.hub_events
    written = 0
    for row in (await conn.execute(_candidates(project_id, cutoff).order_by(tables.plans.c.plan_id))).all():
        steps = runs.steps_to_go(row.body if isinstance(row.body, dict) else {})
        if not steps:
            continue
        run_id = await _newest_run(conn, project_id, row.plan_id)
        same = {"plan_id": row.plan_id, "revision": row.revision, "run_id": run_id}
        seen = select(e.c.id).where(
            e.c.project_id == project_id, e.c.kind == hub_events.PLAN_STALLED, e.c.payload.contains(same)
        )
        if await conn.scalar(seen.limit(1)) is not None:
            continue
        await ops_events.plan_stalled(
            conn, project_id=project_id, plan_id=row.plan_id, revision=row.revision, run_id=run_id, steps=steps
        )
        written += 1
    return written


async def _queued_long(conn: AsyncConnection, project_id: int, cutoff, quiet_workers: tuple[int, ...] = ()) -> int:
    r, e = tables.runs, tables.hub_events
    told = exists().where(
        e.c.project_id == project_id,
        e.c.kind == hub_events.RUN_QUEUED_LONG,
        e.c.payload["run_id"].as_integer() == r.c.id,
    )
    query = select(r.c.id, r.c.pinned_worker_id, r.c.priority).where(
        r.c.project_id == project_id, r.c.kind == "chat", r.c.state == "queued", r.c.queued_at < cutoff, ~told
    )
    if quiet_workers:  # a run pinned to a quiet worker waits on purpose; one with no pin is judged as any other
        query = query.where(or_(r.c.pinned_worker_id.is_(None), r.c.pinned_worker_id.not_in(quiet_workers)))
    query = query.order_by(r.c.id).limit(MAX_QUEUED)
    rows = (await conn.execute(query)).all()
    for row in rows:
        await ops_events.run_queued_long(
            conn, project_id=project_id, run_id=row.id, worker_id=row.pinned_worker_id, priority=row.priority
        )
    return len(rows)


__all__ = ["LOCK", "MAX_QUEUED", "OWN_KINDS", "check"]
