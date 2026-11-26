"""The queries of the prompt of a chat run (``chats.service.prompts``)."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chat_runs as rules
from evo_agents.hub import tables
from evo_agents.hub.server.chats.repository.views import message_select


async def worker_checkouts(conn: AsyncConnection, run_id: int) -> dict:
    """The checkouts of the worker of run ``run_id``, as it reported them; none without one."""
    r, w = tables.runs, tables.workers
    held_by = select(w.c.checkouts).join_from(r, w, w.c.id == r.c.worker_id).where(r.c.id == run_id)
    return (await conn.execute(held_by)).scalar_one_or_none() or {}


async def chat_title_kind(conn: AsyncConnection, chat_id: int) -> tuple[str, str]:
    """(chat ``chat_id``'s title, its kind)."""
    c = tables.chats
    title, kind = (await conn.execute(select(c.c.title, c.c.kind).where(c.c.id == chat_id))).one()
    return title, kind


async def prompt_rows(conn: AsyncConnection, chat_id: int, root_id: int, *, direct: bool) -> tuple[list, list]:
    """(the messages before the thread of ``root_id`` in chat ``chat_id`` a prompt shows, oldest first; the messages of
    the thread, in order), rows of ``message_select``. Before the thread: in a direct chat the latest turns, the
    messages of the threads before this one; else the messages that start no thread."""
    cm = tables.chat_messages
    thread = message_select().where(cm.c.chat_id == chat_id, or_(cm.c.id == root_id, cm.c.thread_root_id == root_id))
    if direct:  # the latest turns: the messages of the threads before this one, the agent's answers in them too
        earlier = func.coalesce(cm.c.thread_root_id, cm.c.id) < root_id
    else:  # the chat before the thread: the messages that start no thread
        earlier = cm.c.thread_root_id.is_(None) & (cm.c.id < root_id)
    limit = rules.DIRECT_MESSAGES if direct else rules.BEFORE_MESSAGES
    before = message_select().where(cm.c.chat_id == chat_id, earlier).order_by(cm.c.id.desc()).limit(limit + 1)
    before_rows = list(reversed((await conn.execute(before)).all()))
    thread_rows = list((await conn.execute(thread.order_by(cm.c.seq))).all())
    return before_rows, thread_rows
