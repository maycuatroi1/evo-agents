"""The query of the events of a group chat (``chats.service.events``)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def events_of(conn: AsyncConnection, chat_id: int, after: int, limit: int) -> list:
    """The events of chat ``chat_id`` after seq ``after``, at most ``limit``, oldest first: rows (seq, kind, body,
    created_at)."""
    ce = tables.chat_events
    query = (
        select(ce.c.seq, ce.c.kind, ce.c.body, ce.c.created_at)
        .where(ce.c.chat_id == chat_id, ce.c.seq > after)
        .order_by(ce.c.seq)
        .limit(limit)
    )
    return list((await conn.execute(query)).all())
