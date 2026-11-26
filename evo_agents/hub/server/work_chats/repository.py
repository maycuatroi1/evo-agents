"""The rows of work chats: the trace of a run or of a work chat, the chat a trace opens and the lock it opens it under,
the threads of an agent in it, and the messages its lines repeat or its copies copy."""

from __future__ import annotations

from sqlalchemy import and_, exists, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, tables
from evo_agents.hub.server.work_chats.models import Trace


def _trace_query():
    t, s, m = tables.system_traces, tables.agent_systems, tables.chat_messages
    root = func.coalesce(m.c.thread_root_id, m.c.id).label("root_id")
    return select(
        t.c.id,
        t.c.system_id,
        t.c.project_id,
        t.c.chat_id,
        t.c.message_id,
        root,
        t.c.opened_by,
        s.c.entry_profile_id,
        t.c.work_chat_id,
    ).select_from(t.outerjoin(s, s.c.id == t.c.system_id).outerjoin(m, m.c.id == t.c.message_id))


async def trace_of_run(conn: AsyncConnection, run_id: int) -> Trace | None:
    """The trace run ``run_id`` is a span of; None for a run in none."""
    s = tables.system_spans
    span = select(s.c.trace_id).where(s.c.run_id == run_id, s.c.name == "invoke_agent").scalar_subquery()
    row = (await conn.execute(_trace_query().where(tables.system_traces.c.id == span))).one_or_none()
    return None if row is None else Trace(*row)


async def trace_of_chat(conn: AsyncConnection, chat_id: int) -> Trace | None:
    """The trace whose work chat chat ``chat_id`` is; None for any other chat."""
    query = _trace_query().where(tables.system_traces.c.work_chat_id == chat_id)
    row = (await conn.execute(query.limit(1))).one_or_none()
    return None if row is None else Trace(*row)


async def login_of(conn: AsyncConnection, user_id: int) -> str:
    u = tables.users
    return (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()


# Opening the work chat


async def body_of(conn: AsyncConnection, message_id: int) -> str | None:
    """The body of chat message ``message_id``; None when it is gone."""
    m = tables.chat_messages
    return (await conn.execute(select(m.c.body).where(m.c.id == message_id))).scalar_one_or_none()


async def add_chat(conn: AsyncConnection, values: dict) -> int:
    c = tables.chats
    return (await conn.execute(insert(c).values(**values).returning(c.c.id))).scalar_one()


async def lock_trace(conn: AsyncConnection, trace_id: int) -> None:
    """Take the advisory lock of trace ``trace_id`` until the transaction ends, under which its work chat opens."""
    await conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"work:{trace_id}", 0))))


async def work_chat_of(conn: AsyncConnection, trace_id: int) -> int | None:
    t = tables.system_traces
    return (await conn.execute(select(t.c.work_chat_id).where(t.c.id == trace_id))).scalar_one()


async def set_work_chat(conn: AsyncConnection, trace_id: int, chat_id: int) -> None:
    t = tables.system_traces
    await conn.execute(update(t).values(work_chat_id=chat_id).where(t.c.id == trace_id))


# Handing work over


def _threads(chat_id: int, profile_id: int):
    """(the thread root, the brief) of each run of agent ``profile_id`` in chat ``chat_id``, newest first, a thread of
    the Orchestrator left out."""
    r, m, o, b = tables.runs, tables.chat_messages, tables.orchestrator_threads, tables.agent_ask_briefs
    root = func.coalesce(m.c.thread_root_id, m.c.id)
    return (
        select(root, b.c.brief)
        .join_from(r, m, m.c.id == r.c.chat_message_id)
        .outerjoin(b, b.c.run_id == r.c.id)
        .where(r.c.chat_id == chat_id, r.c.profile_id == profile_id, r.c.kind == "chat")
        .where(~exists().where(and_(o.c.chat_id == chat_id, o.c.root_message_id == root)))
        .order_by(r.c.id.desc())
    )


async def latest_thread(conn: AsyncConnection, chat_id: int, profile_id: int) -> int | None:
    """The thread of the latest run of agent ``profile_id`` in chat ``chat_id`` (see ``_threads``); None."""
    return (await conn.execute(_threads(chat_id, profile_id).limit(1))).scalar_one_or_none()


async def threads(conn: AsyncConnection, chat_id: int, profile_id: int, limit: int) -> list:
    """The ``limit`` latest (thread root, brief) of agent ``profile_id`` in chat ``chat_id`` (see ``_threads``)."""
    return list((await conn.execute(_threads(chat_id, profile_id).limit(limit))).all())


async def mark_repeats(conn: AsyncConnection, message_id: int, chat_id: int, repeats: int) -> None:
    """Have message ``message_id`` of chat ``chat_id`` name message ``repeats``, when that is of the same chat and the
    chat is a work chat."""
    m, c = tables.chat_messages, tables.chats
    same = exists().where(m.c.id == repeats, m.c.chat_id == chat_id)
    work = exists().where(c.c.id == chat_id, c.c.kind == chats.WORK)
    await conn.execute(update(m).values(repeats_id=repeats).where(m.c.id == message_id, same, work))


async def copied_from(conn: AsyncConnection, message_id: int) -> int | None:
    """The message chat message ``message_id`` copies; None for one that copies none."""
    m = tables.chat_messages
    return (await conn.execute(select(m.c.copied_from_id).where(m.c.id == message_id))).scalar_one_or_none()
