"""The rows the members' channels of a core plugin read: the member of a direct chat."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, tables


async def direct_user(conn: AsyncConnection, chat_id: int) -> int | None:
    """The member of direct chat ``chat_id``, None for a group chat or no chat."""
    c = tables.chats
    query = select(c.c.direct_user_id).where(c.c.id == chat_id, c.c.kind == chats.DIRECT)
    return (await conn.execute(query)).scalar_one_or_none()
