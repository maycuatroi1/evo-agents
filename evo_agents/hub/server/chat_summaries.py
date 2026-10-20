"""The list of a person's chats (GET /v1/chats, ``chats.list_chats``) with what the web draws for each: its kind, its
image, the agent of a direct chat, the faces of a group chat and its last message.

A group chat shows the image its members set (``chat_images``); without one, the web draws ``faces``: at most
``chats.FACES`` members other than the reader, in the order they were added, an agent with its colour and avatar, a
person with their GitHub avatar. A direct chat shows its agent (``direct_agent``), deleted or not. ``last_message`` is
the latest message of the chat's main line: of a group chat, the latest that starts no thread; of a direct chat, the
latest of all, since the web shows a direct chat's answers under the message they answer. It carries its author and its
text as one short line without markdown (``chats.preview``); a message whose label the reader does not clear keeps its
author and time and leaves its text out (``hidden``), as the messages a reader may not read are left out elsewhere.

The list takes the same few queries however many chats the person is in: the chats, their members, their last
messages, the agents among them, and the projects and grants the labels of those messages name.
"""

from __future__ import annotations

from sqlalchemy import func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, tables
from evo_agents.hub.db import one_of
from evo_agents.hub.server.chat_access import LabelReader, label_reader
from evo_agents.hub.server.chat_views import (
    HUB_AUTHOR,
    ChatAuthor,
    ChatLastMessage,
    ChatSummary,
    agent_author,
    person_author,
    profiles_by_id,
    summaries_query,
)
from evo_agents.hub.server.profile_store import Profile


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


def _face(member, profiles: dict[int, Profile]) -> ChatAuthor:
    if member.profile_id is not None:
        return agent_author(profiles[member.profile_id])
    return person_author(member.login, member.github_id)


def _last(row, profiles: dict[int, Profile], reader: LabelReader) -> ChatLastMessage | None:
    if row is None:
        return None
    if row.author_profile_id is not None:
        author = agent_author(profiles[row.author_profile_id])
    elif row.author_login is not None:
        author = person_author(row.author_login, row.author_github_id)
    else:
        author = HUB_AUTHOR
    seen = reader.sees(row.label)
    return ChatLastMessage(
        id=row.id,
        thread_root_id=row.thread_root_id,
        author=author,
        preview=chats.preview(row.body) if seen else None,
        hidden=not seen,
        created_at=row.created_at,
    )


async def chat_summaries(conn: AsyncConnection, user_id: int) -> list[ChatSummary]:
    """The chats person ``user_id`` is in, the latest active first, each as ``ChatSummary`` shows it to them."""
    rows = (await conn.execute(summaries_query(user_id))).all()
    chat_ids = [row.id for row in rows]
    if not chat_ids:
        return []
    members: dict[int, list] = {chat_id: [] for chat_id in chat_ids}
    for member in await conn.execute(_members_query(chat_ids)):
        members[member.chat_id].append(member)
    last = {row.chat_id: row for row in await conn.execute(_last_query(chat_ids))}
    profiles = await profiles_by_id(
        conn,
        [m.profile_id for found in members.values() for m in found if m.profile_id is not None]
        + [row.direct_profile_id for row in rows if row.direct_profile_id is not None]
        + [row.author_profile_id for row in last.values() if row.author_profile_id is not None],
    )
    reader = await label_reader(conn, user_id, [row.label for row in last.values()])
    found = []
    for row in rows:
        here = members[row.id]
        others = [member for member in here if member.user_id != user_id]
        found.append(
            ChatSummary(
                id=row.id,
                title=row.title,
                created_by=row.created_by,
                created_at=row.created_at,
                last_message_at=row.last_message_at,
                last_seq=row.last_seq,
                unread=row.unread,
                people=[member.login for member in here if member.user_id is not None],
                agents=[profiles[member.profile_id].handle for member in here if member.profile_id is not None],
                kind=row.kind,
                image_url=chats.image_url(row.id, row.image_sha256),
                direct_agent=agent_author(profiles[row.direct_profile_id]) if row.direct_profile_id else None,
                faces=[_face(member, profiles) for member in others[: chats.FACES]],
                last_message=_last(last.get(row.id), profiles, reader),
            )
        )
    return found
