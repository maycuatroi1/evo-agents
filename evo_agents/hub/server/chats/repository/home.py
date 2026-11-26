"""The queries of the chat home (``chats.service.home``)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.profile_store import Profile, profile_of, profile_select


async def candidates(conn: AsyncConnection, user_id: int) -> list[Profile]:
    """The agents not deleted of the projects ``user_id`` holds a grant on."""
    ap, g = tables.agent_profiles, tables.grants
    projects = select(g.c.project_id).where(g.c.user_id == user_id)
    query = profile_select().where(ap.c.project_id.in_(projects), ap.c.deleted_at.is_(None))
    return [profile_of(row) for row in await conn.execute(query)]


async def direct_chats(conn: AsyncConnection, user_id: int) -> dict[int, tuple[int, datetime | None]]:
    """{agent id: (the id of ``user_id``'s direct chat with it, when they last wrote in it)}."""
    c, m = tables.chats, tables.chat_messages
    last = (
        select(func.max(m.c.created_at)).where(m.c.chat_id == c.c.id, m.c.author_user_id == user_id).scalar_subquery()
    )
    query = select(c.c.direct_profile_id, c.c.id, last.label("last")).where(c.c.direct_user_id == user_id)
    return {row.direct_profile_id: (row.id, row.last) for row in await conn.execute(query)}
