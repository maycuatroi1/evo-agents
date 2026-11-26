"""The query of whom a person may add to a group chat (``chats.service.people``)."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.chats.models.people import MAX_CANDIDATES


async def people_seen(conn: AsyncConnection, user_id: int) -> list:
    """Everyone who holds a grant on a project ``user_id`` holds one on, and everyone in a chat they are in, them left
    out, by login ignoring case, at most ``MAX_CANDIDATES``: rows (id, login, github_id)."""
    u, g, m = tables.users, tables.grants, tables.chat_members
    mine = select(g.c.project_id).where(g.c.user_id == user_id)
    sharing = select(g.c.user_id).where(g.c.project_id.in_(mine))
    my_chats = select(m.c.chat_id).where(m.c.user_id == user_id, m.c.removed_at.is_(None))
    chatting = select(m.c.user_id).where(m.c.chat_id.in_(my_chats), m.c.removed_at.is_(None), m.c.user_id.is_not(None))
    query = (
        select(u.c.id, u.c.login, u.c.github_id)
        .where(or_(u.c.id.in_(sharing), u.c.id.in_(chatting)), u.c.id != user_id)
        .order_by(func.lower(u.c.login))
        .limit(MAX_CANDIDATES)
    )
    return list((await conn.execute(query)).all())
