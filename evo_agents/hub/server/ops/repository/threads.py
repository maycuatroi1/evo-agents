"""Every query of the ops threads (``ops.service.threads`` is the design): the default system of a project, the batches
of its queue and their items, the runs of its ops threads and what they spent, the notices its owner got, and what
the digest counts."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables, triage_rules
from evo_agents.hub.server import system_traces
from evo_agents.hub.server.ops.models.threads import (
    CAP_HOURS,
    DIGEST_HOURS,
    DIGEST_NOTICE,
    RECURRING_DAYS,
    OpsSystem,
    lead_of,
)


async def default_of(conn: AsyncConnection, project_id: int, *, lock: bool = False) -> OpsSystem | None:
    """The default system of project ``project_id``, locked when ``lock``; None when it has none."""
    s, p = tables.agent_systems, tables.projects
    query = (
        select(
            s.c.id,
            s.c.slug,
            s.c.revision,
            s.c.spec,
            s.c.ops_paused_at,
            s.c.ops_failures,
            func.coalesce(s.c.created_by, p.c.created_by).label("owner"),
        )
        .join_from(s, p, p.c.id == s.c.project_id)
        .where(s.c.project_id == project_id, s.c.is_default)
    )
    if lock:
        query = query.with_for_update(of=s)
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return None
    ref = system_traces.SystemRef(row.id, row.slug, project_id, row.revision, row.spec)
    return OpsSystem(ref, row.ops_paused_at is not None, row.ops_failures, row.owner, lead_of(row.spec))


async def ops_projects(conn: AsyncConnection) -> list[int]:
    """The projects whose default system turns ops on, by id."""
    s = tables.agent_systems
    on = select(s.c.project_id).where(s.c.is_default, s.c.spec["ops"]["enabled"].as_boolean().is_(True))
    return sorted((await conn.execute(on)).scalars().all())


def thread_runs(system_id: int):
    """The runs of the ops threads of system ``system_id``: id, state, failure cause, the thread's id."""
    r, m, t = tables.runs, tables.chat_messages, tables.orchestrator_threads
    return (
        select(r.c.id, r.c.state, r.c.failure_cause, t.c.id.label("thread_id"))
        .select_from(
            r.join(m, m.c.id == r.c.chat_message_id).join(
                t,
                and_(t.c.chat_id == r.c.chat_id, t.c.root_message_id == func.coalesce(m.c.thread_root_id, m.c.id)),
            )
        )
        .where(t.c.system_id == system_id, t.c.rule == triage_rules.OPS)
    )


async def spent(conn: AsyncConnection, system_id: int) -> float:
    """What the runs of the ops threads of system ``system_id`` queued in the last CAP_HOURS spent (run_spend)."""
    rs, r = tables.run_spend, tables.runs
    inside = thread_runs(system_id).where(r.c.queued_at >= func.now() - timedelta(hours=CAP_HOURS)).subquery()
    cost = func.coalesce(func.sum(func.coalesce(rs.c.reported_usd, rs.c.estimated_usd)), 0)
    query = select(cost).where(rs.c.run_id.in_(select(inside.c.id)))
    return float((await conn.execute(query)).scalar_one() or 0)


async def thread_rows(conn: AsyncConnection, system_id: int, thread_id: int) -> list:
    """The runs of ops thread ``thread_id`` of system ``system_id``, the first first."""
    inside = thread_runs(system_id).where(tables.orchestrator_threads.c.id == thread_id).subquery()
    return list((await conn.execute(select(inside).order_by(inside.c.id))).all())


async def told(conn: AsyncConnection, owner: int, kind: str, project_id: int, *, hours: int | None) -> bool:
    """Whether member ``owner`` got a notice of ``kind`` of the project in the last ``hours`` hours, or today (UTC)
    for None."""
    n = tables.notifications
    since = func.date_trunc("day", func.now()) if hours is None else func.now() - timedelta(hours=hours)
    query = select(n.c.id).where(
        n.c.user_id == owner, n.c.notice_kind == kind, n.c.project_id == project_id, n.c.created_at >= since
    )
    return (await conn.execute(query.limit(1))).first() is not None


# The summary of a batch


async def pending_items(conn: AsyncConnection, batch_id: int) -> list:
    """The pending items of batch ``batch_id``, with their run, agent, decision, worker and event."""
    i, r, ap, d, w = tables.ops_items, tables.runs, tables.agent_profiles, tables.decisions, tables.workers
    e = tables.hub_events
    query = (
        select(
            i.c.id,
            i.c.kind,
            i.c.cause,
            i.c.subject,
            i.c.run_id,
            i.c.decision_id,
            i.c.worker_id,
            r.c.kind.label("run_kind"),
            r.c.state.label("run_state"),
            r.c.error,
            r.c.priority,
            ap.c.handle,
            d.c.question,
            d.c.category,
            w.c.name.label("worker"),
            e.c.payload,
        )
        .select_from(
            i.outerjoin(r, r.c.id == i.c.run_id)
            .outerjoin(ap, ap.c.id == r.c.profile_id)
            .outerjoin(d, d.c.id == i.c.decision_id)
            .outerjoin(w, w.c.id == i.c.worker_id)
            .outerjoin(e, e.c.id == i.c.event_id)
        )
        .where(i.c.batch_id == batch_id, i.c.state == "pending")
        .order_by(i.c.id)
    )
    return list((await conn.execute(query)).all())


async def last_trace(conn: AsyncConnection, project_id: int, plan_id: str):
    """The newest trace of plan ``plan_id`` of the project, with its system's slug; None for none."""
    t, s = tables.system_traces, tables.agent_systems
    query = (
        select(t.c.id, t.c.last_at, s.c.slug)
        .join_from(t, s, s.c.id == t.c.system_id)
        .where(t.c.project_id == project_id, t.c.plan_id == plan_id)
        .order_by(t.c.id.desc())
        .limit(1)
    )
    return (await conn.execute(query)).first()


async def recurring_times(conn: AsyncConnection, project_id: int, key: str) -> int:
    """How many batches of cause key ``key`` of the project closed in the last RECURRING_DAYS days with items left."""
    b = tables.ops_batches
    query = select(func.count()).where(
        b.c.project_id == project_id,
        b.c.key == key,
        b.c.state.in_(("ready", "running", "done")),
        b.c.closed_at >= func.now() - timedelta(days=RECURRING_DAYS),
    )
    return int((await conn.execute(query)).scalar_one())


# The batches


async def batches(conn: AsyncConnection, project_id: int, state: str) -> list:
    """The batches of the project in ``state``, the first first, locked; one another pass holds skipped."""
    b = tables.ops_batches
    query = select(b).where(b.c.project_id == project_id, b.c.state == state).order_by(b.c.id)
    return list((await conn.execute(query.with_for_update(skip_locked=True))).all())


async def set_running(conn: AsyncConnection, batch_id: int, run_id: int, thread_id: int, recurring: bool) -> None:
    b = tables.ops_batches
    values = {"state": "running", "run_id": run_id, "thread_id": thread_id, "recurring": recurring}
    await conn.execute(update(b).values(**values).where(b.c.id == batch_id))


async def set_done(conn: AsyncConnection, batch_id: int) -> None:
    b = tables.ops_batches
    await conn.execute(update(b).values(state="done").where(b.c.id == batch_id))


async def set_failures(conn: AsyncConnection, system_id: int, failures: int) -> None:
    """Set the count of ops runs in a row of system ``system_id`` that failed of their agent."""
    s = tables.agent_systems
    await conn.execute(update(s).values(ops_failures=failures).where(s.c.id == system_id))


# The digest


async def digest_since(conn: AsyncConnection, owner: int, project_id: int) -> datetime:
    """When the last digest of the project went to ``owner``, DIGEST_HOURS ago at most."""
    n = tables.notifications
    last = select(func.max(n.c.created_at)).where(
        n.c.user_id == owner, n.c.notice_kind == DIGEST_NOTICE, n.c.project_id == project_id
    )
    found = (await conn.execute(last)).scalar_one_or_none()
    floor = (await conn.execute(select(func.now() - timedelta(hours=DIGEST_HOURS)))).scalar_one()
    return floor if found is None or found < floor else found


async def ready_count(conn: AsyncConnection, project_id: int) -> int:
    """How many batches of the project wait for their thread."""
    b = tables.ops_batches
    held = select(func.count()).where(b.c.project_id == project_id, b.c.state == "ready")
    return int((await conn.execute(held)).scalar_one())


async def open_decisions(conn: AsyncConnection, project_id: int) -> list:
    """(id, question) of each open decision an Orchestrator of the project's ops threads asked."""
    t, d = tables.orchestrator_threads, tables.decisions
    asked = (
        select(d.c.id, d.c.question)
        .join_from(d, t, t.c.orchestrator_run_id == d.c.run_id)
        .where(t.c.project_id == project_id, t.c.rule == triage_rules.OPS, d.c.state == "open")
        .order_by(d.c.id)
    )
    return list((await conn.execute(asked)).all())


async def left_count(conn: AsyncConnection, project_id: int) -> int:
    """How many items of the project's done batches the Orchestrator's runs left pending."""
    i, b = tables.ops_items, tables.ops_batches
    left = (
        select(func.count())
        .select_from(i.join(b, b.c.id == i.c.batch_id))
        .where(i.c.project_id == project_id, i.c.state == "pending", b.c.state == "done")
    )
    return int((await conn.execute(left)).scalar_one())


async def handled_since(conn: AsyncConnection, project_id: int, since) -> dict[str, int]:
    """How many items of the project were handled since ``since``, by who handled them."""
    i = tables.ops_items
    handled = (
        select(i.c.handled_by, func.count())
        .where(i.c.project_id == project_id, i.c.state == "handled", i.c.handled_at >= since)
        .group_by(i.c.handled_by)
    )
    return {row[0]: row[1] for row in await conn.execute(handled)}


async def threads_since(conn: AsyncConnection, project_id: int, since) -> int:
    """How many ops threads of the project opened since ``since``."""
    t = tables.orchestrator_threads
    threads = select(func.count()).where(
        t.c.project_id == project_id, t.c.rule == triage_rules.OPS, t.c.created_at >= since
    )
    return int((await conn.execute(threads)).scalar_one())
