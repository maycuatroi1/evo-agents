"""The queries of the list of a person's chats (``chats.service.summaries``): the chats, their members, their last
messages and the traces of the work chats among them, a few queries however many chats there are.
"""

from __future__ import annotations

from sqlalchemy import case, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, tables
from evo_agents.hub.db import one_of


def summaries_query(user_id: int):
    """The chats person ``user_id`` is in, the latest active first, with what ``ChatSummary`` shows."""
    c, m, u, cm = tables.chats, tables.chat_members, tables.users, tables.chat_messages
    mine = m.alias("mine")
    last = select(cm.c.created_at).where(cm.c.chat_id == c.c.id).order_by(cm.c.seq.desc()).limit(1).scalar_subquery()
    unread = case(
        (mine.c.last_read_seq >= c.c.event_seq, 0),
        else_=(
            select(func.count())
            .where(
                cm.c.chat_id == c.c.id,
                cm.c.seq > mine.c.last_read_seq,
                cm.c.author_user_id.is_distinct_from(user_id),
            )
            .scalar_subquery()
        ),
    )
    return (
        select(
            c.c.id,
            c.c.title,
            u.c.login.label("created_by"),
            c.c.created_at,
            last.label("last_message_at"),
            c.c.event_seq.label("last_seq"),
            unread.label("unread"),
            c.c.kind,
            c.c.image_sha256,
            c.c.direct_profile_id,
        )
        .select_from(c.join(mine, mine.c.chat_id == c.c.id).join(u, u.c.id == c.c.created_by))
        .where(mine.c.user_id == user_id, mine.c.removed_at.is_(None))
        .order_by(func.coalesce(last, c.c.created_at).desc(), c.c.id.desc())
    )


def _members_query(chat_ids: list[int]):
    m, u = tables.chat_members, tables.users
    return (
        select(m.c.chat_id, m.c.user_id, m.c.profile_id, u.c.login, u.c.github_id)
        .select_from(m.outerjoin(u, u.c.id == m.c.user_id))
        .where(one_of(m.c.chat_id, chat_ids), m.c.removed_at.is_(None))
        .order_by(m.c.chat_id, m.c.added_at, m.c.id)
    )


def _last_query(chat_ids: list[int]):
    """The latest message of the main line of each chat of ``chat_ids``, one row a chat that has one, its text cut to
    ``chats.PREVIEW_SOURCE`` characters: one query, each chat's row found through its index on (chat, seq)."""
    c, cm, u = tables.chats, tables.chat_messages, tables.users
    latest = (
        select(
            cm.c.id,
            cm.c.thread_root_id,
            cm.c.author_user_id,
            cm.c.author_profile_id,
            cm.c.guest_name,
            func.substr(cm.c.body, 1, chats.PREVIEW_SOURCE).label("body"),
            cm.c.label,
            cm.c.created_at,
        )
        .where(cm.c.chat_id == c.c.id, or_(cm.c.thread_root_id.is_(None), c.c.kind == chats.DIRECT))
        .order_by(cm.c.seq.desc())
        .limit(1)
        .lateral("latest")
    )
    return (
        select(
            c.c.id.label("chat_id"),
            latest,
            u.c.login.label("author_login"),
            u.c.github_id.label("author_github_id"),
        )
        .select_from(c.join(latest, true()).outerjoin(u, u.c.id == latest.c.author_user_id))
        .where(one_of(c.c.id, chat_ids))
    )


async def chats_of(conn: AsyncConnection, user_id: int) -> list:
    """The chats person ``user_id`` is in, the latest active first (``summaries_query``)."""
    return list((await conn.execute(summaries_query(user_id))).all())


async def members_of(conn: AsyncConnection, chat_ids: list[int]) -> dict[int, list]:
    """The members of each chat of ``chat_ids``, in the order they were added, by chat id."""
    members: dict[int, list] = {chat_id: [] for chat_id in chat_ids}
    for member in await conn.execute(_members_query(chat_ids)):
        members[member.chat_id].append(member)
    return members


async def last_messages(conn: AsyncConnection, chat_ids: list[int]) -> dict:
    """The latest message of the main line of each chat of ``chat_ids`` that has one (``_last_query``), by chat id."""
    return {row.chat_id: row for row in await conn.execute(_last_query(chat_ids))}


async def work_traces(conn: AsyncConnection, chat_ids: list[int]) -> dict[int, int]:
    """{chat id: the first trace whose work chat it is} of the work chats of ``chat_ids``."""
    t = tables.system_traces
    query = select(t.c.work_chat_id, func.min(t.c.id)).where(one_of(t.c.work_chat_id, chat_ids))
    return {key: value for key, value in await conn.execute(query.group_by(t.c.work_chat_id))}
