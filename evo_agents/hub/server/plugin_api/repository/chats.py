"""The query of a page of one thread of a chat (``service.chats``)."""

from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.chats.repository.views import message_select


async def thread_rows(conn: AsyncConnection, chat_id: int, root: int, before: int | None, limit: int):
    """The latest ``limit`` messages of the thread of ``root`` in chat ``chat_id`` before seq ``before``, newest
    first, and one more when there is."""
    cm = tables.chat_messages
    query = message_select().where(cm.c.chat_id == chat_id, or_(cm.c.id == root, cm.c.thread_root_id == root))
    if before is not None:
        query = query.where(cm.c.seq < before)
    return (await conn.execute(query.order_by(cm.c.seq.desc()).limit(limit + 1))).all()


__all__ = ["thread_rows"]
