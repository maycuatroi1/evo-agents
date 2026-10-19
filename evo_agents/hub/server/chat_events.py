"""What happens in a group chat, as its members read it: GET /v1/chats/{id}/events?after=SEQ and the live stream GET
/v1/chats/{id}/stream.

Each change of a chat is an event numbered by the chat (``chat_store``): a message, a reaction put or taken off, a
member added or removed. A message event carries the message as it reads now (``chat_views.GroupMessage``); a reaction
event the emoji, who put it or took it off and how many put it after the change; a member event who was added or
removed and by whom. A member reads the events of messages whose label they clear, and the reaction events of those
messages; the others are left out, and the numbers go on past them.

The stream is server-sent events, as a run's is (``run_events``): each event has its seq as id, a reconnecting
client's Last-Event-ID (or ``after`` on the first connection) says where to go on, FastAPI sends a ``: ping`` comment
after 15 idle seconds, and the answer carries ``Cache-Control: no-cache, no-transform``. A stream holds no database
connection while it waits: ``ChatStreams`` wakes it when CHANNEL is notified with its chat's id, on the api process's
one LISTEN connection (``listen``), and it looks again every STREAM_POLL_SECONDS besides. Each look checks the reader
is still a member who clears the chat's label: once they are not, an ``end`` event says why and the stream closes.

Asked for ``progress``, the stream also sends a ``progress`` event with no id (``chat_views.ChatProgressNow``): the
progress of the agents' runs on their way under the messages they took, as the reader may read them
(``chat_progress``), at once and then after each look that found it changed, so it stays out of Last-Event-ID and the
chat's numbering. GET .../events?progress=true answers the same with its events.
"""

from __future__ import annotations

from collections.abc import AsyncIterable
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.chat_access import ChatAccess, chat_access, label_reader
from evo_agents.hub.server.chat_members import REFUSALS, ChatId
from evo_agents.hub.server.chat_progress import by_message, chat_progress
from evo_agents.hub.server.chat_store import CHANNEL
from evo_agents.hub.server.chat_views import (
    ChatEvent,
    ChatEvents,
    ChatMemberChange,
    ChatProgress,
    ChatProgressNow,
    ChatReactionChange,
    message_rows,
    messages_view,
)
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.listen import Listener
from evo_agents.hub.server.run_events import JSON_ERROR, RunStreams, StreamRoute
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.security import CurrentUser, Principal

STREAM_POLL_SECONDS = 5.0  # a stream looks again this often, notification or not
STREAM_BATCH = 200  # events a stream reads at a time
MAX_EVENTS_PAGE = 500
MESSAGE_EVENTS = ("message",)
REACTION_EVENTS = ("reaction_added", "reaction_removed")
PROGRESS_PARAM = "also the progress of the agents' runs on their way under the messages they took (ChatProgress)"

router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})
stream_router = APIRouter(
    prefix="/v1/chats",
    tags=["chats"],
    route_class=StreamRoute,
    responses={401: {"description": "Unauthorized", **JSON_ERROR}},
)


class ChatStreams(RunStreams):
    """The streams open in this api process, by chat, woken by CHANNEL on the process's one LISTEN."""

    def __init__(self, listener: Listener):
        super().__init__(listener, CHANNEL)


def _event(row, messages: dict) -> ChatEvent:
    event = ChatEvent(seq=row.seq, kind=row.kind, created_at=row.created_at)
    if row.kind in MESSAGE_EVENTS:
        event.message = messages[row.body["message_id"]]
    elif row.kind in REACTION_EVENTS:
        event.reaction = ChatReactionChange.model_validate(row.body)
    else:
        event.member = ChatMemberChange.model_validate(row.body)
    return event


async def events_after(
    conn: AsyncConnection, chat: ChatAccess, user: Principal, after: int, limit: int
) -> tuple[list[ChatEvent], int, list[ChatProgress] | None]:
    """(the events of the chat after seq ``after``, at most ``limit`` read, that its reader ``user`` may see; the seq
    of the last event read, ``after`` when none was; the chat's progress, read for the messages among them, None when
    there was none)."""
    ce = tables.chat_events
    query = (
        select(ce.c.seq, ce.c.kind, ce.c.body, ce.c.created_at)
        .where(ce.c.chat_id == chat.chat_id, ce.c.seq > after)
        .order_by(ce.c.seq)
        .limit(limit)
    )
    rows = (await conn.execute(query)).all()
    about = {row.body["message_id"] for row in rows if row.kind in MESSAGE_EVENTS + REACTION_EVENTS}
    found = await message_rows(conn, chat.chat_id, sorted(about))
    reader = await label_reader(conn, chat.user_id, [row.label for row in found])
    shown = {row.id: row for row in found if reader.sees(row.label)}
    new = [shown[r.body["message_id"]] for r in rows if r.kind in MESSAGE_EVENTS and r.body["message_id"] in shown]
    progress = await chat_progress(conn, user, chat.chat_id) if new else None
    viewed = await messages_view(conn, new, reader, by_message(progress or []))
    messages = {message.id: message for message in viewed}
    events = [
        _event(row, messages)
        for row in rows
        if row.kind not in MESSAGE_EVENTS + REACTION_EVENTS or row.body["message_id"] in shown
    ]
    return events, rows[-1].seq if rows else after, progress


@router.get("/{chat_id}/events", response_model=ChatEvents, responses=REFUSALS)
async def list_events(
    request: Request,
    chat_id: ChatId,
    user: CurrentUser,
    after: Annotated[int, Query(ge=0, le=MAX_ID, description="the seq the events start after")] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_EVENTS_PAGE)] = MAX_EVENTS_PAGE,
    progress: Annotated[bool, Query(description=PROGRESS_PARAM)] = False,
) -> ChatEvents:
    """The chat's events after ``after``, oldest first; ``more`` when there are later ones. With ``progress``, the
    progress of the agents' runs on their way under the messages they took, as the reader may read them."""
    async with request.app.state.engine.begin() as conn:
        chat = await chat_access(conn, user, chat_id)
        events, last, found = await events_after(conn, chat, user, after, limit)
        if progress and found is None:
            found = await chat_progress(conn, user, chat_id)
    now = found if progress else None
    return ChatEvents(events=events, last_seq=chat.event_seq, more=last < chat.event_seq, progress=now)


async def _streamed_chat(request: Request, chat_id: ChatId, user: CurrentUser) -> tuple[int, Principal]:
    """The chat a stream follows and its reader, checked before the stream starts so a refusal is an ordinary error
    answer."""
    async with request.app.state.engine.begin() as conn:
        await chat_access(conn, user, chat_id)
    return chat_id, user


@stream_router.get(
    "/{chat_id}/stream",
    response_class=EventSourceResponse,
    responses={403: {"description": "Forbidden", **JSON_ERROR}, 404: {"description": "Not Found", **JSON_ERROR}},
)
async def stream(
    request: Request,
    followed: Annotated[tuple[int, Principal], Depends(_streamed_chat)],
    last_event_id: Annotated[
        int | None, Header(alias="Last-Event-ID", ge=0, le=MAX_ID, description="sent by a reconnecting client")
    ] = None,
    after: Annotated[int, Query(ge=0, le=MAX_ID, description="where a first connection starts")] = 0,
    progress: Annotated[bool, Query(description=PROGRESS_PARAM)] = False,
) -> AsyncIterable[ServerSentEvent]:
    """The chat's events as server-sent events, each with its seq as id; an ``end`` event once the reader may no
    longer read the chat. A ``: ping`` comment keeps an idle stream open. With ``progress``, a ``progress`` event too
    (``ChatProgressNow``, no id): at once, then each time the progress of the agents' runs under their messages
    changed."""
    chat_id, user = followed
    streams: ChatStreams = request.app.state.chat_streams
    streams.start()
    engine = request.app.state.engine
    seen = last_event_id if last_event_id is not None else after
    sent: list[ChatProgress] | None = None  # the progress this stream sent last
    with streams.subscribe(chat_id) as wakeup:
        while True:
            refusal, now = None, None
            async with engine.begin() as conn:
                try:
                    chat = await chat_access(conn, user, chat_id)
                    events, last, now = await events_after(conn, chat, user, seen, STREAM_BATCH)
                    if progress and now is None:
                        now = await chat_progress(conn, user, chat_id)
                except HTTPException as refused:
                    refusal = refused.detail
            if refusal is not None:
                yield ServerSentEvent(event="end", data={"reason": refusal, "last_seq": seen})
                return
            for event in events:
                yield ServerSentEvent(data=event, id=str(event.seq))
            if progress and now != sent:
                yield ServerSentEvent(event="progress", data=ChatProgressNow(progress=now or []))
                sent = now
            full = last - seen >= STREAM_BATCH
            seen = last
            if not full:
                await wakeup.wait(STREAM_POLL_SECONDS)
