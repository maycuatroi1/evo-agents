"""The queries of the level of a group chat's label (``chats.service.labels``)."""

from __future__ import annotations

from sqlalchemy import null, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def people_in(conn: AsyncConnection, chat_id: int) -> dict[int, str]:
    """{user id: login} of the people in chat ``chat_id``."""
    m, u = tables.chat_members, tables.users
    query = select(u.c.id, u.c.login).join_from(m, u, u.c.id == m.c.user_id)
    return dict((await conn.execute(query.where(m.c.chat_id == chat_id, m.c.removed_at.is_(None)))).all())


async def set_override(conn: AsyncConnection, chat_id: int, override: dict | None) -> None:
    """Make ``override`` (None: none) the level chat ``chat_id``'s label is set to."""
    c = tables.chats
    await conn.execute(
        update(c).values(label_override=null() if override is None else override).where(c.c.id == chat_id)
    )
