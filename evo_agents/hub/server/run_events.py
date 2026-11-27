"""A run's log, its live stream, the owner's messages to its agent, and the log and diff its worker uploads.
``docs/workers.md`` is the protocol; ``runs`` holds the queue and ``run_state`` the moves, which write ``state``
events of their own.

POST /v1/worker/runs/{id}/events takes a batch of at most MAX_BATCH_EVENTS events and MAX_BATCH_BYTES of request
from the worker that claimed the run, while it holds the run and after (its spool may be sent once the run ended).
Each event carries the worker's own ``seq``, 1 for the run's first and one more for each next. ``runs.events_acked``
is the highest of those stored with none missing below, answered as ``ack_seq``: an event at or below it is a resend
and is skipped, and one after a gap is not stored, so the daemon sends again from ``ack_seq + 1``. Events stored after
the run ended write its tool figures again (``tool_stats.record``). The hub numbers what it stores itself
(``run_events.seq``, which ``runs.event_seq`` counts): the worker's events and its own (``state`` on each move,
``user_message`` for each message), under the run's row lock, so a reader never sees a number before the ones below
it. A NUL character in a body is stored as U+FFFD (``runs.without_nul``), since jsonb holds none. A body whose JSON
is over MAX_EVENT_BODY_BYTES is cut (``runs.fit_event_body``) and the event marked truncated. A run keeps at most MAX_RUN_EVENTS events of the worker and the owner: a batch that would
pass it stores nothing and gets 413 with the ack in its detail; the hub's state events are written past it. A note
that names a step of a chat run's setup (``chat_runs.SETUP_FIELD``) wakes its chat's streams, as the worker taking the
run's inbox does, since the thread shows that step (``chats.service.progress``).

Readers of the project read a run's events (GET .../runs/{id}/events?after=SEQ) and stream them (GET
.../runs/{id}/stream), for the runs of plans they may read (``runs.readable_run``). The stream is server-sent events
through FastAPI's EventSourceResponse: each event has the hub's seq as its id, a reconnecting client's Last-Event-ID
(or ``after`` on the first connection) says where to go on, FastAPI sends a ``: ping`` comment after 15 idle seconds,
and an ``end`` event closes the stream once the run is final and every event of it was sent. The answer carries
``Cache-Control: no-cache, no-transform``, so a proxy between the hub and the browser neither caches nor compresses
it: a compressing one holds the events back until its buffer fills. A stream holds no database connection while it
waits: ``RunStreams`` wakes it when EVENTS_CHANNEL is notified with its run's id, on the api process's one LISTEN
connection (``listen``), and it looks again every STREAM_POLL_SECONDS besides.

The owner sends the run's agent a message (POST .../runs/{id}/messages, at most MAX_MESSAGE_BYTES of UTF-8) while the
run is queued or held (an author run also while parked: the message is its owner's reply in the chat, and the hub
queues the run that resumes it on the same worker, whose inbox takes it, as an answer to a parked plan run's decision
does; and not once the owner ended its chat), from a web session when the run is on a worker set to take runs
dispatched from the web only (``runs.web_only_steering``: a token gets 403 then): it waits in run_inbox, the log gets
a ``user_message`` event, and the heartbeat counts it until the worker takes it with POST
/v1/worker/runs/{id}/inbox, which also acknowledges the ones handed to the agent.
The owner's answer to a decision of the run comes the same way (``decisions``), as a message that names the decision
(``decision_id``); acknowledging it records when the answer reached the agent (``decisions.delivered_at``).

When the run ends, its worker uploads the whole log (blob kind run-log) and the diff of its commits (run-diff)
through the blob store's uploads and commit (POST /v1/worker/runs/{id}/uploads and /blobs, as the worker's owner,
who must still hold writer); the run records each blob, and readers get a presigned URL of the diff from GET
.../runs/{id}/diff.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.routing import APIRoute
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs
from evo_agents.hub.blobs import GET_TTL
from evo_agents.hub.server import tool_stats
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.blobs import Mismatch, UploadItem, Uploads, blob_store, commit_uploads, issue_uploads
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.listen import Listener
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.run_state import EVENTS_CHANNEL
from evo_agents.hub.server.runs.models import REFUSALS, RunId
from evo_agents.hub.server.runs.models.run_events import (
    BATCH_TOO_LARGE,
    MAX_EVENTS_PAGE,
    MAX_SEQ,
    RUN_BLOB_KINDS,
    RunFull,
)
from evo_agents.hub.server.runs.models.run_events import DiffLink as DiffLink
from evo_agents.hub.server.runs.models.run_events import EventBatch as EventBatch
from evo_agents.hub.server.runs.models.run_events import EventIn as EventIn
from evo_agents.hub.server.runs.models.run_events import EventsAck as EventsAck
from evo_agents.hub.server.runs.models.run_events import Inbox as Inbox
from evo_agents.hub.server.runs.models.run_events import InboxAck as InboxAck
from evo_agents.hub.server.runs.models.run_events import InboxMessage as InboxMessage
from evo_agents.hub.server.runs.models.run_events import Message as Message
from evo_agents.hub.server.runs.models.run_events import MessageIn as MessageIn
from evo_agents.hub.server.runs.models.run_events import RunBlobCommit as RunBlobCommit
from evo_agents.hub.server.runs.models.run_events import RunBlobs as RunBlobs
from evo_agents.hub.server.runs.models.run_events import RunEvent as RunEvent
from evo_agents.hub.server.runs.models.run_events import RunEvents as RunEvents
from evo_agents.hub.server.runs.repository.run_events import deliver, undelivered  # noqa: F401 (local_runs)
from evo_agents.hub.server.runs.service import run_events as service
from evo_agents.hub.server.runs.service.run_events import (  # noqa: F401 (attachments, autonomy, outputs, merge, chats, decisions)
    claimed_run,
    write_user_message,
)
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

STREAM_POLL_SECONDS = 5.0  # a stream looks again this often, notification or not
EVENT_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 413, 422)}


STREAM_CACHE_CONTROL = "no-cache, no-transform"  # FastAPI's own is no-cache


class StreamRoute(APIRoute):
    """Answers a stream of server-sent events with STREAM_CACHE_CONTROL in place of FastAPI's Cache-Control."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def streamed(request: Request):
            response = await handler(request)
            if response.headers.get("content-type", "").startswith("text/event-stream"):
                response.headers["Cache-Control"] = STREAM_CACHE_CONTROL
            return response

        return streamed


class BatchRoute(APIRoute):
    """Reads at most MAX_BATCH_BYTES of a request before FastAPI parses it: a larger one gets 413, whether it says its
    length or not, and is never held in memory whole."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            refusal = error_response(request, 413, BATCH_TOO_LARGE, detail=[{"limit": "batch_bytes"}])
            declared = request.headers.get("content-length")
            if declared is not None and (not declared.isdigit() or int(declared) > runs.MAX_BATCH_BYTES):
                return refusal
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > runs.MAX_BATCH_BYTES:
                    return refusal
                chunks.append(chunk)
            request._body = b"".join(chunks)  # what Request.body() returns from now on
            return await handler(request)

        return bounded


router = APIRouter(prefix="/v1/projects", tags=["runs"], responses={401: {"model": ErrorBody}})
# The stream answers text/event-stream, and FastAPI would document its errors with that type too: they are JSON.
JSON_ERROR = {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorBody"}}}}
stream_router = APIRouter(
    prefix="/v1/projects",
    tags=["runs"],
    route_class=StreamRoute,
    responses={401: {"description": "Unauthorized", **JSON_ERROR}},
)
worker_router = APIRouter(
    prefix="/v1/worker", tags=["worker protocol"], route_class=BatchRoute, responses={401: {"model": ErrorBody}}
)

# Models of the uploads (the others are in runs.models.run_events; these extend the blob store's UploadItem)


class RunBlobItem(UploadItem):
    kind: Literal[RUN_BLOB_KINDS]


class RunUploadRequest(BaseModel):
    items: list[RunBlobItem] = Field(min_length=1, max_length=len(RUN_BLOB_KINDS), description="one blob per kind")


# Streams


class Wakeup:
    """What one stream waits on: set by a notification of its run, cleared when the stream looks again."""

    def __init__(self):
        self._event = asyncio.Event()

    def set(self) -> None:
        self._event.set()

    async def wait(self, timeout: float) -> None:
        """Return once set, or after ``timeout`` seconds; cleared either way before the stream reads again, so a
        notification after this moment wakes the next wait."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except TimeoutError:
            pass
        self._event.clear()


class RunStreams:
    """The streams open in this api process, by run, woken by EVENTS_CHANNEL on the process's one LISTEN; on another
    ``channel``, the streams of what that channel's notifications name by id (``chats.service.events.ChatStreams``)."""

    def __init__(self, listener: Listener, channel: str = EVENTS_CHANNEL):
        self._listener = listener
        self._waiting: dict[int, set[Wakeup]] = {}
        listener.on(channel, self._notified)

    def start(self) -> None:
        self._listener.start()

    def _notified(self, payload: str | None) -> None:
        if payload is None:  # connected again: every stream looks
            targets = [wakeup for group in self._waiting.values() for wakeup in group]
        elif payload.isdigit():
            targets = list(self._waiting.get(int(payload), ()))
        else:
            return
        for wakeup in targets:
            wakeup.set()

    @contextmanager
    def subscribe(self, run_id: int) -> Iterator[Wakeup]:
        wakeup = Wakeup()
        self._waiting.setdefault(run_id, set()).add(wakeup)
        try:
            yield wakeup
        finally:
            group = self._waiting.get(run_id, set())
            group.discard(wakeup)
            if not group:
                self._waiting.pop(run_id, None)

    def open_streams(self) -> int:
        return sum(len(group) for group in self._waiting.values())


# The worker's side


def refused(request: Request, answer):
    """``answer`` of the service, or the 413 answer of a run past its limit (``RunFull``)."""
    if isinstance(answer, RunFull):
        return error_response(request, 413, answer.message, detail=answer.detail)
    return answer


async def store_events(
    request: Request, conn: AsyncConnection, run_id: int, state: str, last_seq: int, acked: int, events: list[EventIn]
):
    """Store the events of a batch that follow the ``acked`` ones of run ``run_id`` (in ``state``, its row locked by
    the caller, ``last_seq`` the hub's last number), in the caller's transaction: an ``EventsAck``, or the 413 answer
    for a run that would pass its limit. The worker's route and the local run's route (``local_runs``) both store
    through it."""
    sealer = request.app.state.sealer
    stored = await service.store_events(conn, sealer, run_id, state, last_seq, acked, events, tool_stats.record)
    return refused(request, stored)


@worker_router.post("/runs/{run_id}/events", response_model=EventsAck, responses=EVENT_REFUSALS)
async def post_events(request: Request, run_id: RunId, body: EventBatch, user: CurrentUser):
    """Store the events of the batch that follow the ones stored, and say up to where the worker's spool may go."""
    state = request.app.state
    return refused(
        request, await service.post_events(state.engine, state.sealer, user, run_id, body.events, tool_stats.record)
    )


@worker_router.post("/runs/{run_id}/inbox", response_model=Inbox, responses=REFUSALS)
async def take_inbox(request: Request, run_id: RunId, user: CurrentUser, body: InboxAck | None = None) -> Inbox:
    """Mark the messages up to ``ack`` delivered, and hand out the ones that still wait, for a run the worker holds."""
    return await service.take_inbox(request.app.state.engine, user, run_id, (body or InboxAck()).ack)  # pyright: ignore[reportCallIssue]


@worker_router.post(
    "/runs/{run_id}/uploads",
    response_model=Uploads,
    responses={code: {"model": ErrorBody} for code in (403, 404, 413, 422, 503)},
)
async def request_run_uploads(request: Request, run_id: RunId, body: RunUploadRequest, user: CurrentUser):
    """Presigned PUT URLs for the log (run-log) and the diff (run-diff) of a run the worker claimed, as its owner."""
    kinds = [item.kind for item in body.items]
    if len(set(kinds)) != len(kinds):
        raise HTTPException(422, "a run has one log and one diff: name each kind once")
    project = await service.claimed_project(request.app.state.engine, user, run_id)
    return await issue_uploads(request, user, project, list(body.items))


@worker_router.post(
    "/runs/{run_id}/blobs",
    response_model=RunBlobs,
    responses={code: {"model": ErrorBody} for code in (403, 404, 422, 503)},
)
async def commit_run_blobs(request: Request, run_id: RunId, body: RunBlobCommit, user: CurrentUser):
    """Commit the uploads of a run's log and diff, and record them on the run."""
    ids = sorted({str(upload_id) for upload_id in body.upload_ids})
    project, _ = await service.committed_kinds(request.app.state.engine, user, run_id, ids)
    try:
        committed = await commit_uploads(request, user, project, ids, kinds=frozenset(RUN_BLOB_KINDS))
    except Mismatch as exc:
        message = (
            f"{len(exc.problems)} upload(s) do not match what was declared, such as {exc.problems[0]['upload_id']}: "
            f"{exc.problems[0]['problem']}. Nothing was committed and the uploads were discarded; ask for new ones"
        )
        return error_response(request, 422, message, detail=exc.problems)
    return await service.record_blobs(request.app.state.engine, user, run_id, committed.blobs)


# The readers' side


@router.get(
    "/{project}/runs/{run_id}/events",
    response_model=RunEvents,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def list_events(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    user: CurrentUser,
    after: Annotated[int, Query(ge=0, le=MAX_SEQ, description="the seq of the last event the caller has")] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_EVENTS_PAGE)] = 500,
    kind: Annotated[list[Literal[runs.EVENT_KINDS]], Query(description="only these kinds; repeat it")] = [],  # noqa: B006
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> RunEvents:
    """The run's events after ``after``, in seq order."""
    engine, kinds = request.app.state.engine, list(dict.fromkeys(kind))
    return await service.list_events(engine, user, project, run_id, after=after, kinds=kinds, limit=limit, sink=sink)


async def _streamed_run(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> int:
    """The run a stream follows, checked before the stream starts so a refusal is an ordinary error answer."""
    await service.check_readable(request.app.state.engine, user, project, run_id, sink)
    return run_id


@stream_router.get(
    "/{project}/runs/{run_id}/stream",
    response_class=EventSourceResponse,
    responses={403: {"description": "Forbidden", **JSON_ERROR}, 404: {"description": "Not Found", **JSON_ERROR}},
)
async def stream(
    request: Request,
    followed: Annotated[int, Depends(_streamed_run)],
    user: CurrentUser,
    last_event_id: Annotated[
        int | None, Header(alias="Last-Event-ID", ge=0, le=MAX_SEQ, description="sent by a reconnecting client")
    ] = None,
    after: Annotated[int, Query(ge=0, le=MAX_SEQ, description="where a first connection starts")] = 0,
) -> AsyncIterable[ServerSentEvent]:
    """The run's events as server-sent events, each with its seq as id, then an ``end`` event once the run is final
    and every event was sent; a ``: ping`` comment keeps an idle stream open. The stream of a run's token
    (``run_access``) stops once the token no longer holds, which each look checks (``security.still_live``), and the
    client that connects again is answered 401."""
    streams: RunStreams = request.app.state.run_streams
    streams.start()
    seen = last_event_id if last_event_id is not None else after
    async for event in service.follow(request.app.state.engine, streams, user, followed, seen, STREAM_POLL_SECONDS):
        yield event


# The owner's messages


@router.post(
    "/{project}/runs/{run_id}/messages",
    status_code=201,
    response_model=Message,
    responses={**REFUSALS, 413: {"model": ErrorBody}},
)
async def send_message(request: Request, project: ProjectName, run_id: RunId, body: MessageIn, user: CurrentUser):
    """Leave a message for the run's agent in its inbox, which the worker hands to the agent; a reply to a parked
    author run queues the run that resumes it, whose inbox takes the message."""
    return refused(request, await service.send_message(request.app.state.engine, user, project, run_id, body))


# The diff


@router.get(
    "/{project}/runs/{run_id}/diff",
    response_model=DiffLink,
    responses={code: {"model": ErrorBody} for code in (403, 404, 503)},
)
async def diff(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    user: CurrentUser,
    download: Annotated[bool, Query(description="ask the store to answer as an attachment, run-<id>.diff")] = False,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> DiffLink:
    """A presigned GET of the diff the run's worker uploaded when the run ended."""
    store = blob_store(request)
    sha256, size = await service.diff_of(request.app.state.engine, user, project, run_id, sink)
    expires_at = datetime.now(UTC) + GET_TTL  # taken before signing, so never later than the URL
    filename = f"run-{run_id}.diff" if download else None
    url = await asyncio.to_thread(store.presign_get, sha256, filename=filename)
    return DiffLink(sha256=sha256, size=size, url=url, expires_at=expires_at)
