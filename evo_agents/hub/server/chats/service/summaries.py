"""The list of a person's chats (GET /v1/chats, ``chats.routes.chats.list_chats``) with what the web draws for each: its
kind, its image, the agent of a direct chat, the faces of a group chat and its last message.

A group chat shows the image its members set (``chats.service.images``); without one, the web draws ``faces``: at most
``chats.FACES`` members other than the reader, in the order they were added, an agent with its colour and avatar, a
person with their GitHub avatar. A direct chat shows its agent (``direct_agent``), deleted or not. ``last_message`` is
the latest message of the chat's main line: of a group chat, the latest that starts no thread; of a direct chat, the
latest of all, since the web shows a direct chat's answers under the message they answer. It carries its author and its
text as one short line without markdown (``chats.preview``); a message whose label the reader does not clear keeps its
author and time and leaves its text out (``hidden``), as the messages a reader may not read are left out elsewhere.

A work chat (``chats.WORK``) carries the work of its trace (``work``: its system, title, status and what it waits for,
``work_status.briefs``). The list takes the same few queries however many chats the person is in: the chats, their
members, their last messages, the agents among them, the projects and grants the labels of those messages name, and
the traces of the work chats among them.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import chats
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.chats.models.access import LabelReader
from evo_agents.hub.server.chats.models.views import (
    ChatAuthor,
    ChatLastMessage,
    ChatSummary,
    agent_author,
    message_author,
    person_author,
)
from evo_agents.hub.server.chats.repository import summaries as store
from evo_agents.hub.server.chats.repository.views import profiles_by_id
from evo_agents.hub.server.chats.service.access import label_reader
from evo_agents.hub.server.profile_store import Profile
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.work_status import briefs
from evo_agents.hub.server.work_status_models import WorkBrief


def _face(member, profiles: dict[int, Profile]) -> ChatAuthor:
    if member.profile_id is not None:
        return agent_author(profiles[member.profile_id])
    return person_author(member.login, member.github_id)


def _last(row, profiles: dict[int, Profile], reader: LabelReader) -> ChatLastMessage | None:
    if row is None:
        return None
    author = message_author(row, profiles)
    seen = reader.sees(row.label)
    return ChatLastMessage(
        id=row.id,
        thread_root_id=row.thread_root_id,
        author=author,
        preview=chats.preview(row.body) if seen else None,
        hidden=not seen,
        created_at=row.created_at,
    )


async def _work(conn: AsyncConnection, chat_ids: list[int]) -> dict[int, WorkBrief]:
    """The work of each work chat of ``chat_ids`` (``work_status.briefs``), by chat id."""
    if not chat_ids:
        return {}
    traces = await store.work_traces(conn, chat_ids)
    found = await briefs(conn, list(traces.values()))
    return {chat_id: found[trace] for chat_id, trace in traces.items() if trace in found}


async def chat_summaries(conn: AsyncConnection, user_id: int) -> list[ChatSummary]:
    """The chats person ``user_id`` is in, the latest active first, each as ``ChatSummary`` shows it to them."""
    rows = await store.chats_of(conn, user_id)
    chat_ids = [row.id for row in rows]
    if not chat_ids:
        return []
    members = await store.members_of(conn, chat_ids)
    last = await store.last_messages(conn, chat_ids)
    profiles = await profiles_by_id(
        conn,
        [m.profile_id for found in members.values() for m in found if m.profile_id is not None]
        + [row.direct_profile_id for row in rows if row.direct_profile_id is not None]
        + [row.author_profile_id for row in last.values() if row.author_profile_id is not None],
    )
    reader = await label_reader(conn, user_id, [row.label for row in last.values()])
    work = await _work(conn, [row.id for row in rows if row.kind == chats.WORK])
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
                work=work.get(row.id),
            )
        )
    return found


async def list_chats(engine: AsyncEngine, user: Principal) -> list[ChatSummary]:
    """The chats ``user`` is in, as ``chat_summaries`` reads them, in a transaction of their own."""
    async with UnitOfWork(engine) as conn:
        return await chat_summaries(conn, user.user_id)
