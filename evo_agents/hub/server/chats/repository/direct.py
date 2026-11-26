"""The queries of direct chats (``chats.service.direct``)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, tables
from evo_agents.hub.server.profile_store import Profile


async def found(conn: AsyncConnection, user_id: int, profile_id: int) -> int | None:
    """The id of person ``user_id``'s direct chat with agent ``profile_id``, or None."""
    c = tables.chats
    query = select(c.c.id).where(c.c.direct_user_id == user_id, c.c.direct_profile_id == profile_id)
    return (await conn.execute(query)).scalar_one_or_none()


async def insert_direct(conn: AsyncConnection, user_id: int, profile: Profile) -> int | None:
    """Insert the direct chat of person ``user_id`` and agent ``profile``; its id, or None when another request made it
    first."""
    c = tables.chats
    values = {"title": profile.name[: chats.MAX_TITLE_CHARS], "created_by": user_id, "kind": chats.DIRECT}
    statement = (
        pg_insert(c)
        .values(**values, direct_user_id=user_id, direct_profile_id=profile.id)
        .on_conflict_do_nothing(index_elements=[c.c.direct_user_id, c.c.direct_profile_id])
        .returning(c.c.id)
    )
    return (await conn.execute(statement)).scalar_one_or_none()


async def direct_agent(conn: AsyncConnection, chat_id: int):
    """The agent of direct or settle chat ``chat_id``: its row (id, handle)."""
    c, ap = tables.chats, tables.agent_profiles
    query = select(ap.c.id, ap.c.handle).join_from(c, ap, ap.c.id == c.c.direct_profile_id)
    return (await conn.execute(query.where(c.c.id == chat_id))).one()
