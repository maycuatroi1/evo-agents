"""The queries of group chats and their messages (``chats.service.chats``, ``chats.service.messages``)."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.db import one_of
from evo_agents.hub.server.chats.repository.views import message_select


async def page_rows(conn: AsyncConnection, chat_id: int, before: int | None, thread: int | None, limit: int) -> list:
    """The latest ``limit`` + 1 messages (``message_select``) of the chat before seq ``before``, or of the thread of
    ``thread``, the latest first."""
    cm = tables.chat_messages
    query = message_select().where(cm.c.chat_id == chat_id)
    if before is not None:
        query = query.where(cm.c.seq < before)
    if thread is not None:
        query = query.where(or_(cm.c.id == thread, cm.c.thread_root_id == thread))
    return list((await conn.execute(query.order_by(cm.c.seq.desc()).limit(limit + 1))).all())


async def members_by_name(conn: AsyncConnection, chat_id: int) -> dict[str, dict]:
    """The members of the chat by the name a mention resolves: a person's login (lowered), an agent's handle."""
    m, u, ap = tables.chat_members, tables.users, tables.agent_profiles
    query = (
        select(m.c.user_id, m.c.profile_id, u.c.login, ap.c.handle, ap.c.is_default, ap.c.deleted_at)
        .select_from(m.outerjoin(u, u.c.id == m.c.user_id).outerjoin(ap, ap.c.id == m.c.profile_id))
        .where(m.c.chat_id == chat_id, m.c.removed_at.is_(None))
    )
    found = {}
    for row in await conn.execute(query):
        if row.user_id is not None:
            found[row.login.lower()] = {"user_id": row.user_id, "via": "login"}
        else:
            via = "project" if row.is_default else "handle"
            deleted = row.deleted_at is not None
            found[row.handle] = {"profile_id": row.profile_id, "via": via, "deleted": deleted}
    return found


async def outsiders(conn: AsyncConnection, user_id: int, names: list[str]) -> dict[str, int]:
    """The agents ``names`` names on a project person ``user_id`` holds a grant on: handle to profile id."""
    ap, g = tables.agent_profiles, tables.grants
    query = (
        select(ap.c.handle, ap.c.id)
        .join_from(ap, g, (g.c.project_id == ap.c.project_id) & (g.c.user_id == user_id))
        .where(one_of(ap.c.handle, names))
    )
    return {row.handle: row.id for row in await conn.execute(query)}


async def people_count(conn: AsyncConnection, chat_id: int) -> int:
    """How many people are in the chat now."""
    m = tables.chat_members
    people = select(func.count()).where(m.c.chat_id == chat_id, m.c.user_id.is_not(None), m.c.removed_at.is_(None))
    return (await conn.execute(people)).scalar_one()


async def others_in_thread(conn: AsyncConnection, root: int, user_id: int) -> int:
    """How many messages of the thread of ``root`` a person other than ``user_id`` wrote."""
    cm = tables.chat_messages
    others = select(func.count()).where(
        or_(cm.c.id == root, cm.c.thread_root_id == root),
        cm.c.author_user_id.is_not(None),
        cm.c.author_user_id != user_id,
    )
    return (await conn.execute(others)).scalar_one()


async def handle_of(conn: AsyncConnection, profile_id: int) -> str:
    """The handle of agent ``profile_id``."""
    ap = tables.agent_profiles
    return (await conn.execute(select(ap.c.handle).where(ap.c.id == profile_id))).scalar_one()


async def member_handle(conn: AsyncConnection, chat_id: int, profile_id: int) -> str | None:
    """The handle of agent ``profile_id`` when it is in the chat now and not deleted; None otherwise."""
    m, ap = tables.chat_members, tables.agent_profiles
    query = (
        select(ap.c.handle)
        .join_from(m, ap, ap.c.id == m.c.profile_id)
        .where(m.c.chat_id == chat_id, m.c.profile_id == profile_id, m.c.removed_at.is_(None))
        .where(ap.c.deleted_at.is_(None))
    )
    return (await conn.execute(query)).scalar_one_or_none()


async def event_seq(conn: AsyncConnection, chat_id: int) -> int:
    """The chat's event seq now."""
    c = tables.chats
    return (await conn.execute(select(c.c.event_seq).where(c.c.id == chat_id))).scalar_one()


async def unread(conn: AsyncConnection, chat_id: int, last: int, user_id: int) -> int:
    """How many messages of the chat after seq ``last`` someone other than person ``user_id`` wrote."""
    cm = tables.chat_messages
    query = select(func.count()).where(
        cm.c.chat_id == chat_id, cm.c.seq > last, cm.c.author_user_id.is_distinct_from(user_id)
    )
    return (await conn.execute(query)).scalar_one()
