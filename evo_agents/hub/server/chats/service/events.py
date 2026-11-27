"""The events of a group chat as its members read them (``chats.routes.events`` has the design): the page of events
after a seq, with the messages they carry as their reader sees them, and the streams of the api process, woken by
CHANNEL (``chats.repository.store``).
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import chats
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.chats.models.access import ChatAccess
from evo_agents.hub.server.chats.models.events import (
    MEMBER_EVENTS,
    MESSAGE_EVENTS,
    REACTION_EVENTS,
    STREAM_BATCH,
    STREAM_TOKEN_ENDED,
)
from evo_agents.hub.server.chats.models.views import (
    ChatAuthor,
    ChatEvent,
    ChatEvents,
    ChatImageChange,
    ChatMemberChange,
    ChatProgress,
    ChatReactionChange,
)
from evo_agents.hub.server.chats.repository import events as store
from evo_agents.hub.server.chats.repository.store import CHANNEL
from evo_agents.hub.server.chats.repository.views import message_rows, person_avatars
from evo_agents.hub.server.chats.service.access import chat_access, label_reader
from evo_agents.hub.server.chats.service.progress import by_message, chat_progress
from evo_agents.hub.server.chats.service.views import messages_view
from evo_agents.hub.server.listen import Listener
from evo_agents.hub.server.run_events import RunStreams
from evo_agents.hub.server.security import Principal, still_live


class ChatStreams(RunStreams):
    """The streams open in this api process, by chat, woken by CHANNEL on the process's one LISTEN."""

    def __init__(self, listener: Listener):
        super().__init__(listener, CHANNEL)


def _people(rows) -> set[str]:
    """The logins of the people the reaction and member events of ``rows`` name."""
    found = set()
    for row in rows:
        named = row.body.get("by") if row.kind in REACTION_EVENTS else row.body.get("member")
        if row.kind in REACTION_EVENTS + MEMBER_EVENTS and isinstance(named, dict) and named.get("kind") == "person":
            found.add(named["login"])
    return found


def _faced(author: ChatAuthor, avatars: dict[str, str | None]) -> None:
    if author.kind == "person" and author.login is not None:
        author.avatar_url = avatars.get(author.login.lower())


def _event(row, chat_id: int, messages: dict, avatars: dict[str, str | None]) -> ChatEvent:
    event = ChatEvent(seq=row.seq, kind=row.kind, created_at=row.created_at)  # pyright: ignore[reportCallIssue]
    if row.kind in MESSAGE_EVENTS:
        event.message = messages[row.body["message_id"]]
    elif row.kind in REACTION_EVENTS:
        event.reaction = ChatReactionChange.model_validate(row.body)
        _faced(event.reaction.by, avatars)
    elif row.kind == chats.IMAGE_CHANGED:
        event.image = ChatImageChange(image_url=chats.image_url(chat_id, row.body["sha256"]), by=row.body["by"])
    else:
        event.member = ChatMemberChange.model_validate(row.body)
        _faced(event.member.member, avatars)
    return event


async def events_after(
    conn: AsyncConnection, chat: ChatAccess, user: Principal, after: int, limit: int
) -> tuple[list[ChatEvent], int, list[ChatProgress] | None]:
    """(the events of the chat after seq ``after``, at most ``limit`` read, that its reader ``user`` may see; the seq
    of the last event read, ``after`` when none was; the chat's progress, read for the messages among them, None when
    there was none)."""
    rows = await store.events_of(conn, chat.chat_id, after, limit)
    about = {row.body["message_id"] for row in rows if row.kind in MESSAGE_EVENTS + REACTION_EVENTS}
    found = await message_rows(conn, chat.chat_id, sorted(about))
    reader = await label_reader(conn, chat.user_id, [row.label for row in found])
    shown = {row.id: row for row in found if reader.sees(row.label)}
    new = [shown[r.body["message_id"]] for r in rows if r.kind in MESSAGE_EVENTS and r.body["message_id"] in shown]
    progress = await chat_progress(conn, user, chat.chat_id) if new else None
    viewed = await messages_view(conn, new, reader, by_message(progress or []))
    messages = {message.id: message for message in viewed}
    avatars = await person_avatars(conn, _people(rows))
    events = [
        _event(row, chat.chat_id, messages, avatars)
        for row in rows
        if row.kind not in MESSAGE_EVENTS + REACTION_EVENTS or row.body["message_id"] in shown
    ]
    return events, rows[-1].seq if rows else after, progress


async def chat_events(
    engine: AsyncEngine, user: Principal, chat_id: int, after: int, limit: int, *, progress: bool
) -> ChatEvents:
    """The events of chat ``chat_id`` after ``after`` that ``user`` reads, at most ``limit``; with ``progress``, the
    progress of the agents' runs on their way under the messages they took, as ``user`` may read them."""
    async with UnitOfWork(engine) as conn:
        chat = await chat_access(conn, user, chat_id)
        events, last, found = await events_after(conn, chat, user, after, limit)
        if progress and found is None:
            found = await chat_progress(conn, user, chat_id)
        now = found if progress else None
        return ChatEvents(events=events, last_seq=chat.event_seq, more=last < chat.event_seq, progress=now)


async def look(
    conn: AsyncConnection, user: Principal, chat_id: int, seen: int, limit: int, *, progress: bool
) -> tuple[list[ChatEvent], int, list[ChatProgress] | None]:
    """A look of a stream of chat ``chat_id`` for ``user`` (``events_after`` from ``seen``), with the chat's progress
    when asked for it; the refusals of ``chat_access`` once ``user`` may no longer read it."""
    chat = await chat_access(conn, user, chat_id)
    events, last, now = await events_after(conn, chat, user, seen, limit)
    if progress and now is None:
        now = await chat_progress(conn, user, chat_id)
    return events, last, now


class ChatLooks:
    """The looks of one stream of chat ``chat_id`` for ``user``, each one short transaction of its own. Each reads one
    snapshot (REPEATABLE READ): under read committed, a reader who left between the check of access and the read of
    the events would get the event of their leaving before the end."""

    def __init__(self, engine: AsyncEngine, user: Principal, chat_id: int, *, progress: bool):
        self.engine = engine.execution_options(isolation_level="REPEATABLE READ")
        self.user = user
        self.chat_id = chat_id
        self.progress = progress

    async def after(self, seen: int) -> tuple[str | None, list[ChatEvent], int, list[ChatProgress] | None]:
        """(why the stream ends, None while ``user`` still reads the chat with a live token; the events after
        ``seen``, at most STREAM_BATCH read; the seq of the last one read; the chat's progress when asked for)."""
        async with UnitOfWork(self.engine) as conn:
            try:
                if not await still_live(conn, self.user):
                    raise HTTPException(401, STREAM_TOKEN_ENDED)
                events, last, now = await look(
                    conn, self.user, self.chat_id, seen, STREAM_BATCH, progress=self.progress
                )
            except HTTPException as refused:
                return refused.detail, [], seen, None
        return None, events, last, now
