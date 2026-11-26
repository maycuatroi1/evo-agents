"""The queries of the group chats a direct chat opens (``chats.service.groups``)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, tables


async def found(conn: AsyncConnection, root: int | None) -> tuple[int, str] | None:
    """(id, title) of the group chat opened from the thread of message ``root``, or None."""
    if root is None:
        return None
    c = tables.chats
    row = (await conn.execute(select(c.c.id, c.c.title).where(c.c.opened_from_message_id == root))).one_or_none()
    return None if row is None else (row.id, row.title)


async def insert_group(conn: AsyncConnection, user_id: int, root: int | None, title: str) -> int | None:
    """Insert the group chat ``title`` of person ``user_id`` for the thread of ``root``; its id, or None when another
    request made it first."""
    c = tables.chats
    statement = pg_insert(c).values(title=title, created_by=user_id, kind=chats.GROUP, opened_from_message_id=root)
    if root is not None:
        statement = statement.on_conflict_do_nothing(
            index_elements=[c.c.opened_from_message_id], index_where=c.c.opened_from_message_id.is_not(None)
        )
    return (await conn.execute(statement.returning(c.c.id))).scalar_one_or_none()


async def direct_profile_id(conn: AsyncConnection, chat_id: int) -> int:
    """The id of the agent of direct chat ``chat_id``."""
    c = tables.chats
    return (await conn.execute(select(c.c.direct_profile_id).where(c.c.id == chat_id))).scalar_one()


async def message_body(conn: AsyncConnection, message_id: int) -> str:
    """The text of message ``message_id``."""
    m = tables.chat_messages
    return (await conn.execute(select(m.c.body).where(m.c.id == message_id))).scalar_one()


async def has_agent(conn: AsyncConnection, chat_id: int, handle: str) -> bool:
    """Whether agent ``handle`` is in chat ``chat_id`` now."""
    m, ap = tables.chat_members, tables.agent_profiles
    query = (
        select(m.c.id)
        .join_from(m, ap, ap.c.id == m.c.profile_id)
        .where(m.c.chat_id == chat_id, ap.c.handle == handle, m.c.removed_at.is_(None))
    )
    return (await conn.execute(query)).first() is not None
