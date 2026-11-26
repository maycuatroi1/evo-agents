"""The rows of the threads the hub opens to an Orchestrator (``service.threads``): the threads, the ops chat of a
system, and what the rules of progress read of a trace."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import BigInteger, and_, exists, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, system_context, tables, triage_rules

# the states of a run that should move: a run that waits for an answer, parked or waiting, is quiet by design
MOVING_STATES = ("queued", "leased", "running", "verifying")
RECOVERABLE = (triage_rules.FAILED, triage_rules.STALE)  # the rules whose thread a later done run of its agent closes


async def login(conn: AsyncConnection, user_id: int) -> str:
    u = tables.users
    return (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()


async def run_origin(conn: AsyncConnection, run_id: int, machine: str):
    """(who dispatched run ``run_id``, its credential kind, ``machine`` for none, its hop)."""
    r = tables.runs
    query = select(r.c.dispatched_by, func.coalesce(r.c.dispatched_via, machine), func.coalesce(r.c.hop, 1))
    return (await conn.execute(query.where(r.c.id == run_id))).one()


async def add_chat(conn: AsyncConnection, title: str, user_id: int) -> int:
    c = tables.chats
    return (await conn.execute(insert(c).values(title=title, created_by=user_id).returning(c.c.id))).scalar_one()


async def ops_row(conn: AsyncConnection, system_id: int | None):
    """The creator and the ops chat of system ``system_id``, under the lock of its ops chat."""
    s = tables.agent_systems
    await conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"ops:{system_id}", 0))))
    return (await conn.execute(select(s.c.created_by, s.c.ops_chat_id).where(s.c.id == system_id))).one()


async def set_ops_chat(conn: AsyncConnection, system_id: int | None, chat_id: int) -> None:
    s = tables.agent_systems
    await conn.execute(update(s).values(ops_chat_id=chat_id).where(s.c.id == system_id))


async def add_thread(conn: AsyncConnection, values: dict) -> int | None:
    """Insert the thread ``values``; its id, or None when its key has a thread already."""
    t = tables.orchestrator_threads
    statement = pg_insert(t).values(**values)
    if values.get("key") is not None:
        statement = statement.on_conflict_do_nothing(index_elements=[t.c.key], index_where=t.c.key.is_not(None))
    return (await conn.execute(statement.returning(t.c.id))).scalar_one_or_none()


async def placed(conn: AsyncConnection, thread_id: int, chat_id: int, root_id: int, run_id: int | None) -> None:
    """Keep where thread ``thread_id`` opened and the Orchestrator's run on it."""
    t = tables.orchestrator_threads
    values = {"chat_id": chat_id, "root_message_id": root_id, "orchestrator_run_id": run_id}
    await conn.execute(update(t).values(**values).where(t.c.id == thread_id))


async def set_wait(conn: AsyncConnection, thread_id: int, wait_id: int) -> None:
    t = tables.orchestrator_threads
    await conn.execute(update(t).values(wait_id=wait_id).where(t.c.id == thread_id))


async def evidence(conn: AsyncConnection, run_id: int) -> tuple[int, ...]:
    """The invoke spans of run ``run_id``."""
    s = tables.system_spans
    query = select(s.c.id).where(s.c.run_id == run_id, s.c.name == system_context.INVOKE)
    return tuple((await conn.execute(query)).scalars().all())


async def run_error(conn: AsyncConnection, run_id: int) -> str | None:
    r = tables.runs
    return (await conn.execute(select(r.c.error).where(r.c.id == run_id))).scalar_one()


async def add_run_triage(conn: AsyncConnection, values: dict) -> None:
    await conn.execute(insert(tables.run_triage).values(**values))


async def received(conn: AsyncConnection, trace_id: int, handle: str, resume: str) -> tuple[list, list]:
    """(the invoke spans of agent ``handle`` in trace ``trace_id`` that go on in no session, whose attributes have no
    ``resume``, with their message and the goal of their brief; the ok ask spans of the trace that made a run of that
    agent, with their message)."""
    s, m, b = tables.system_spans, tables.chat_messages, tables.agent_ask_briefs
    query = (
        select(s.c.id, m.c.body, b.c.brief["goal"].astext, s.c.message_id)
        .select_from(s.outerjoin(m, m.c.id == s.c.message_id).outerjoin(b, b.c.run_id == s.c.run_id))
        .where(
            s.c.trace_id == trace_id,
            s.c.name == system_context.INVOKE,
            s.c.agent == handle,
            ~s.c.attributes.has_key(resume),
        )
        .order_by(s.c.id)
    )
    invoked = (await conn.execute(query)).all()
    r, ap = tables.runs, tables.agent_profiles
    child = s.c.attributes["evo.child_run_id"].astext.cast(BigInteger)
    asks = (
        select(s.c.id, m.c.body, s.c.message_id)
        .select_from(
            s.join(r, r.c.id == child).join(ap, ap.c.id == r.c.profile_id).outerjoin(m, m.c.id == s.c.message_id)
        )
        .where(s.c.trace_id == trace_id, s.c.tool == system_context.TOOL_ASK, s.c.status == "ok", ap.c.handle == handle)
    )
    return list(invoked), list(await conn.execute(asks))


def _moving(s):
    r = tables.runs
    return exists().where(r.c.id == s.c.run_id, r.c.state.in_(MOVING_STATES))


async def idle_traces(conn: AsyncConnection, limit: int) -> list:
    """The traces with a running invoke span of a run that should move (MOVING_STATES) and no new span for a minute,
    the least idle limit, with no idle thread; oldest first, at most ``limit``."""
    t, s, o = tables.system_traces, tables.system_spans, tables.orchestrator_threads
    running = exists().where(
        s.c.trace_id == t.c.id, s.c.name == system_context.INVOKE, s.c.status == "running", _moving(s)
    )
    told = exists().where(o.c.key == func.concat(f"{triage_rules.IDLE}:trace:", t.c.id))
    query = (
        select(t.c.id, t.c.last_at, func.now().label("now"))
        .where(running, ~told, t.c.system_id.is_not(None), t.c.last_at < func.now() - timedelta(minutes=1))
        .order_by(t.c.last_at)
    )
    return list((await conn.execute(query.limit(limit))).all())


async def running_spans(conn: AsyncConnection, trace_id: int) -> list:
    """The running invoke spans of trace ``trace_id`` of runs that should move, the newest first."""
    s = tables.system_spans
    query = (
        select(s.c.id, s.c.run_id, s.c.runtime)
        .where(s.c.trace_id == trace_id, s.c.name == system_context.INVOKE, s.c.status == "running", _moving(s))
        .order_by(s.c.id.desc())
    )
    return list((await conn.execute(query)).all())


async def recovered_threads(conn: AsyncConnection, limit: int) -> list:
    """The open threads of RECOVERABLE with no outcome whose agent has a run in the same trace, after the one the
    thread is about, that ended done, and whose Orchestrator is not at work on them; with the latest such run."""
    o, s, r = tables.orchestrator_threads, tables.system_spans, tables.runs
    later = (
        select(func.max(s.c.run_id))
        .join(r, r.c.id == s.c.run_id)
        .where(
            s.c.trace_id == o.c.trace_id,
            s.c.name == system_context.INVOKE,
            s.c.agent == o.c.subject,
            s.c.run_id > o.c.subject_run_id,
            r.c.state == "done",
        )
        .scalar_subquery()
    )
    lead = r.alias("lead_run")
    busy = exists().where(lead.c.id == o.c.orchestrator_run_id, lead.c.state.in_(runs.ACTIVE_STATES))
    query = (
        select(o.c.id, o.c.trace_id, o.c.chat_id, o.c.root_message_id, o.c.subject, o.c.subject_run_id)
        .add_columns(later.label("run_id"))
        .where(
            o.c.rule.in_(RECOVERABLE),
            o.c.outcome.is_(None),
            o.c.closed_at.is_(None),
            o.c.trace_id.is_not(None),
            o.c.subject_run_id.is_not(None),
            ~busy,
            later.is_not(None),
        )
        .order_by(o.c.id)
    )
    return list((await conn.execute(query.limit(limit))).all())


async def close_open(conn: AsyncConnection, thread_id: int) -> bool:
    """Close thread ``thread_id`` when it is open with no outcome; whether it did."""
    t = tables.orchestrator_threads
    still = t.c.closed_at.is_(None), t.c.outcome.is_(None)
    statement = update(t).where(t.c.id == thread_id, *still).values(closed_at=func.now()).returning(t.c.id)
    return (await conn.execute(statement)).scalar_one_or_none() is not None


async def thread_at(conn: AsyncConnection, chat_id: int, root_id: int, *, lock: bool = False):
    """The row of the thread whose first message is ``root_id`` of chat ``chat_id``; None for none."""
    t = tables.orchestrator_threads
    query = select(t).where(and_(t.c.chat_id == chat_id, t.c.root_message_id == root_id))
    if lock:
        query = query.with_for_update()
    return (await conn.execute(query)).one_or_none()
