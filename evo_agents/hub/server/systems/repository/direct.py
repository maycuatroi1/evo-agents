"""The rows a direct chat that hands work to an agent system reads (``service.direct``): a system's entry agent and
the chats the work goes through."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def entry_profile(conn: AsyncConnection, system_id: int) -> int | None:
    s = tables.agent_systems
    return (await conn.execute(select(s.c.entry_profile_id).where(s.c.id == system_id))).scalar_one()


async def direct_profile(conn: AsyncConnection, chat_id: int) -> int | None:
    """The agent of direct chat ``chat_id``."""
    c = tables.chats
    return (await conn.execute(select(c.c.direct_profile_id).where(c.c.id == chat_id))).scalar_one()


async def chat_title(conn: AsyncConnection, chat_id: int) -> str:
    c = tables.chats
    return (await conn.execute(select(c.c.title).where(c.c.id == chat_id))).scalar_one()
