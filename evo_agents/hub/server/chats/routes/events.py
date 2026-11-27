"""What happens in a group chat, as its members read it: GET /v1/chats/{id}/events?after=SEQ and the live stream GET
/v1/chats/{id}/stream.

Each change of a chat is an event numbered by the chat (``chats.repository.store``): a message, a reaction put or taken
off, a member added or removed, its image set or taken off, a message that reads otherwise now (``message_changed``:
outputs of its run went under it, ``output_store``). A message event and a message_changed event carry the message as it
reads now (``chats.models.views.GroupMessage``); a reaction event the emoji, who put it or took it off and how many put
it after the change; a member event who was added or removed and by whom; an image event the image's URL now and who
changed it. A person in an event shows with their GitHub avatar as the hub knows it when the event is read. A member
reads the events of messages whose label they clear, and the reaction events of those messages; the others are left out,
and the numbers go on past them.

The stream is server-sent events, as a run's is (``run_events``): each event has its seq as id, a reconnecting
client's Last-Event-ID (or ``after`` on the first connection) says where to go on, FastAPI sends a ``: ping`` comment
after 15 idle seconds, and the answer carries ``Cache-Control: no-cache, no-transform``. A stream holds no database
connection while it waits: ``ChatStreams`` wakes it when CHANNEL is notified with its chat's id, on the api process's
one LISTEN connection (``listen``), and it looks again every STREAM_POLL_SECONDS besides. Each look checks the reader
is still a member who clears the chat's label: once they are not, an ``end`` event says why and the stream closes.

Asked for ``progress``, the stream also sends a ``progress`` event with no id (``chats.models.views.ChatProgressNow``):
the progress of the agents' runs on their way under the messages they took, as the reader may read them
(``chats.service.progress``), at once and then after each look that found it changed, so it stays out of Last-Event-ID
and the chat's numbering. GET .../events?progress=true answers the same with its events.
"""

from __future__ import annotations

from collections.abc import AsyncIterable
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from evo_agents.hub.server.chats.models.events import MAX_EVENTS_PAGE, PROGRESS_PARAM, STREAM_BATCH, STREAM_TOKEN_ENDED
from evo_agents.hub.server.chats.models.members import REFUSALS, ChatId
from evo_agents.hub.server.chats.models.views import ChatEvents, ChatProgress, ChatProgressNow
from evo_agents.hub.server.chats.service.access import check_access
from evo_agents.hub.server.chats.service.events import ChatLooks, ChatStreams, chat_events
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.run_events import JSON_ERROR, StreamRoute
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.security import CurrentUser, Principal

__all__ = ["STREAM_POLL_SECONDS", "STREAM_TOKEN_ENDED", "router", "stream_router"]

STREAM_POLL_SECONDS = 5.0  # a stream looks again this often, notification or not

router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})
stream_router = APIRouter(
    prefix="/v1/chats",
    tags=["chats"],
    route_class=StreamRoute,
    responses={401: {"description": "Unauthorized", **JSON_ERROR}},
)


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
    return await chat_events(request.app.state.engine, user, chat_id, after, limit, progress=progress)


async def _streamed_chat(request: Request, chat_id: ChatId, user: CurrentUser) -> tuple[int, Principal]:
    """The chat a stream follows and its reader, checked before the stream starts so a refusal is an ordinary error
    answer."""
    await check_access(request.app.state.engine, user, chat_id)
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
    changed. The stream of a run's token (``run_access``) ends once the token no longer holds, which each look checks
    (``security.still_live``)."""
    chat_id, user = followed
    streams: ChatStreams = request.app.state.chat_streams
    streams.start()
    looks = ChatLooks(request.app.state.engine, user, chat_id, progress=progress)
    seen = last_event_id if last_event_id is not None else after
    sent: list[ChatProgress] | None = None  # the progress this stream sent last
    with streams.subscribe(chat_id) as wakeup:
        while True:
            refusal, events, last, now = await looks.after(seen)
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
