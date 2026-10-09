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
it. A body whose JSON is over MAX_EVENT_BODY_BYTES is cut (``runs.fit_event_body``) and
the event marked truncated. A run keeps at most MAX_RUN_EVENTS events of the worker and the owner: a batch that would
pass it stores nothing and gets 413 with the ack in its detail; the hub's state events are written past it.

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
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.routing import APIRoute
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import UUID4, AwareDatetime, BaseModel, Field, model_validator
from sqlalchemy import DateTime, and_, bindparam, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.blobs import GET_TTL
from evo_agents.hub.server import audit, tool_stats
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.blobs import Mismatch, UploadItem, Uploads, blob_store, commit_uploads, issue_uploads
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.listen import Listener
from evo_agents.hub.server.run_state import EVENTS_CHANNEL, next_seq, notify_events
from evo_agents.hub.server.runs import (
    MAX_ID,
    NOT_HELD,
    REFUSALS,
    RunId,
    _audit_run,
    _owned_run,
    _run_target,
    readable_run,
    web_only_steering,
)
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

MAX_SEQ = 2**31 - 1  # run_events.seq and runs.events_acked are integers
STREAM_POLL_SECONDS = 5.0  # a stream looks again this often, notification or not
STREAM_BATCH = 500  # events a stream reads at a time
MAX_EVENTS_PAGE = 1000
MAX_INBOX = 100  # messages one POST of the worker's inbox hands out
RUN_BLOB_KINDS = ("run-log", "run-diff")
BLOB_COLUMNS = {"run-log": "log_sha256", "run-diff": "diff_sha256"}
EVENT_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 413, 422)}
BATCH_TOO_LARGE = f"a batch of events is at most {runs.MAX_BATCH_BYTES} bytes of request; send fewer at a time"
RUN_FULL = (
    "run {id} has {count} events, and a run keeps at most {limit} of them: nothing of this batch was stored, and "
    "the worker should send no more of this run's events"
)


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


# Models


class EventIn(BaseModel):
    seq: int = Field(ge=1, le=MAX_SEQ, description="the worker's number of the event: 1 for the run's first")
    at: AwareDatetime = Field(description="when the worker saw it; a time after the hub's now is taken as now")
    kind: Literal[runs.WORKER_EVENT_KINDS] = Field(description="user_message and state are the hub's own")
    body: dict[str, Any] = Field(description="a JSON object; over 64 KiB of JSON, the hub cuts it")


class EventBatch(BaseModel):
    events: list[EventIn] = Field(max_length=runs.MAX_BATCH_EVENTS)


class EventsAck(BaseModel):
    ack_seq: int = Field(description="the worker's highest seq stored with none missing below: drop the spool to it")
    stored: int = Field(description="events of this batch stored now; the others were resends or after a gap")


class RunEvent(BaseModel):
    seq: int = Field(description="the hub's number of the event in the run: events?after= and Last-Event-ID take it")
    at: datetime
    kind: Literal[runs.EVENT_KINDS]
    body: dict[str, Any]
    truncated: bool = Field(description="the hub cut the body to 64 KiB of JSON")


class RunEvents(BaseModel):
    run_id: int
    state: Literal[runs.RUN_STATES]
    last_seq: int = Field(description="the seq of the run's latest event")
    events: list[RunEvent] = Field(description="in seq order")
    more: bool = Field(description="more events follow the last one here: ask again after it")


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=runs.MAX_MESSAGE_BYTES, description="at most 8 KiB of UTF-8")

    @model_validator(mode="after")
    def _bounded(self):
        if not self.text.strip():
            raise ValueError("a message needs some text")
        if len(self.text.encode()) > runs.MAX_MESSAGE_BYTES:
            raise ValueError(f"a message is at most {runs.MAX_MESSAGE_BYTES} bytes of UTF-8")
        return self


class Message(BaseModel):
    id: int
    run_id: int = Field(
        description="the run whose inbox took it: the run messaged, or the run that resumes a parked author run"
    )
    seq: int = Field(description="its user_message event in the run's log")
    text: str
    sent_by: str
    created_at: datetime
    delivered_at: datetime | None = Field(description="when the worker took it for the agent")


class InboxAck(BaseModel):
    ack: int | None = Field(
        None, ge=1, le=MAX_ID, description="the last message handed to the agent: it and the ones before are delivered"
    )


class InboxMessage(BaseModel):
    id: int
    text: str
    sent_by: str
    created_at: datetime
    decision_id: int | None = Field(None, description="the decision this message answers; null for any other message")


class Inbox(BaseModel):
    messages: list[InboxMessage] = Field(description="the messages not delivered yet, oldest first")


class RunBlobItem(UploadItem):
    kind: Literal[RUN_BLOB_KINDS]


class RunUploadRequest(BaseModel):
    items: list[RunBlobItem] = Field(min_length=1, max_length=len(RUN_BLOB_KINDS), description="one blob per kind")


class RunBlobCommit(BaseModel):
    upload_ids: list[UUID4] = Field(min_length=1, max_length=len(RUN_BLOB_KINDS))


class RunBlobs(BaseModel):
    log_sha256: str | None
    diff_sha256: str | None


class DiffLink(BaseModel):
    sha256: str
    size: int | None
    url: str = Field(description="a presigned GET of the diff, working until expires_at")
    expires_at: datetime


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
    """The streams open in this api process, by run, woken by EVENTS_CHANNEL on the process's one LISTEN."""

    def __init__(self, listener: Listener):
        self._listener = listener
        self._waiting: dict[int, set[Wakeup]] = {}
        listener.on(EVENTS_CHANNEL, self._notified)

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


async def _claimed(conn: AsyncConnection, user: Principal, run_id: int):
    """(worker name, state, event_seq, events_acked, project) of run ``run_id``, its row locked, when the worker of
    ``user`` claimed it; 403 for a token of no live worker, 404 for another worker's run or none."""
    w, r, p = tables.workers, tables.runs, tables.projects
    live = select(w.c.id, w.c.name).where(w.c.token_id == user.token_id, w.c.revoked_at.is_(None))
    worker = (await conn.execute(live)).one_or_none()
    if worker is None:
        raise HTTPException(403, "this worker token belongs to no live worker: join the machine again")
    claimed = (
        select(r.c.worker_id, r.c.state, r.c.event_seq, r.c.events_acked, p.c.name.label("project"))
        .join_from(r, p, p.c.id == r.c.project_id)
        .where(r.c.id == run_id)
        .with_for_update(of=r)
    )
    row = (await conn.execute(claimed)).one_or_none()
    if row is None or row.worker_id != worker.id:
        raise HTTPException(404, f"this worker did not claim run {run_id}")
    return (worker.name, row.state, row.event_seq, row.events_acked, row.project)


def _fresh(events: list[EventIn], acked: int) -> list[EventIn]:
    """The events of a batch that come next after ``acked``, in seq order: resends are skipped, and the first gap
    ends what is taken."""
    taken, expected = [], acked + 1
    for event in sorted(events, key=lambda item: item.seq):
        if event.seq < expected:
            continue
        if event.seq > expected:
            break
        taken.append(event)
        expected += 1
    return taken


@worker_router.post("/runs/{run_id}/events", response_model=EventsAck, responses=EVENT_REFUSALS)
async def post_events(request: Request, run_id: RunId, body: EventBatch, user: CurrentUser):
    """Store the events of the batch that follow the ones stored, and say up to where the worker's spool may go."""
    async with request.app.state.engine.begin() as conn:
        _, state, last_seq, acked, _ = await _claimed(conn, user, run_id)
        fresh = _fresh(body.events, acked)
        if not fresh:
            return EventsAck(ack_seq=acked, stored=0)
        if last_seq + len(fresh) > runs.MAX_RUN_EVENTS:
            message = RUN_FULL.format(id=run_id, count=last_seq, limit=runs.MAX_RUN_EVENTS)
            detail = [{"limit": "events_per_run", "max": runs.MAX_RUN_EVENTS, "ack_seq": acked}]
            return error_response(request, 413, message, detail=detail)
        rows = []
        for number, event in enumerate(fresh, start=last_seq + 1):
            stored, cut = runs.fit_event_body(event.body)
            rows.append(
                {
                    "run_id": run_id,
                    "seq": number,
                    "seen_at": event.at,
                    "kind": event.kind,
                    "body": stored,
                    "truncated": cut,
                }
            )
        e, r = tables.run_events, tables.runs
        # A time after the hub's now is taken as now.
        stored_at = func.least(bindparam("seen_at", type_=DateTime(timezone=True)), func.now())
        await conn.execute(insert(e).values(at=stored_at), rows)
        count = update(r).values(event_seq=r.c.event_seq + len(fresh), events_acked=r.c.events_acked + len(fresh))
        await conn.execute(count.where(r.c.id == run_id))
        if state in runs.TERMINAL_STATES:  # the spool's last events, after the end: the figures take them in
            await tool_stats.record(conn, run_id)
        await notify_events(conn, run_id)
    cut = sum(1 for row in rows if row["truncated"])
    log.debug("run events stored", extra={"run_id": run_id, "stored": len(fresh), "truncated": cut})
    return EventsAck(ack_seq=acked + len(fresh), stored=len(fresh))


def _deliver(run_id: int, ack: int):
    """Mark the messages of run ``run_id`` up to ``ack`` delivered, and the decisions they answer, which reached the
    agent with them: one statement, the update of the inbox a data-modifying CTE of the update of decisions."""
    i, d = tables.run_inbox, tables.decisions
    taken = (
        update(i)
        .values(delivered_at=func.now())
        .where(i.c.run_id == run_id, i.c.id <= ack, i.c.delivered_at.is_(None))
        .returning(i.c.decision_id)
        .cte("taken")
    )
    return (
        update(d)
        .values(delivered_at=func.now())
        .where(d.c.id.in_(select(taken.c.decision_id)), d.c.state == "answered", d.c.delivered_at.is_(None))
    )


def _undelivered(run_id: int):
    i, u = tables.run_inbox, tables.users
    return (
        select(i.c.id, i.c.body.label("text"), u.c.login.label("sent_by"), i.c.created_at, i.c.decision_id)
        .join_from(i, u, u.c.id == i.c.sent_by)
        .where(i.c.run_id == run_id, i.c.delivered_at.is_(None))
        .order_by(i.c.id)
        .limit(MAX_INBOX)
    )


@worker_router.post("/runs/{run_id}/inbox", response_model=Inbox, responses=REFUSALS)
async def take_inbox(request: Request, run_id: RunId, user: CurrentUser, body: InboxAck | None = None) -> Inbox:
    """Mark the messages up to ``ack`` delivered, and hand out the ones that still wait, for a run the worker holds."""
    ack = (body or InboxAck()).ack
    async with request.app.state.engine.begin() as conn:
        _, state, *_ = await _claimed(conn, user, run_id)
        if state not in runs.HELD_STATES:
            raise HTTPException(404, NOT_HELD.format(id=run_id))
        if ack is not None:
            await conn.execute(_deliver(run_id, ack))
        rows = (await conn.execute(_undelivered(run_id))).all()
    return Inbox(messages=[InboxMessage(**row._mapping) for row in rows])


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
    async with request.app.state.engine.begin() as conn:
        project = (await _claimed(conn, user, run_id))[4]
    return await issue_uploads(request, user, project, list(body.items))


@worker_router.post(
    "/runs/{run_id}/blobs",
    response_model=RunBlobs,
    responses={code: {"model": ErrorBody} for code in (403, 404, 422, 503)},
)
async def commit_run_blobs(request: Request, run_id: RunId, body: RunBlobCommit, user: CurrentUser):
    """Commit the uploads of a run's log and diff, and record them on the run."""
    ids = sorted({str(upload_id) for upload_id in body.upload_ids})
    async with request.app.state.engine.begin() as conn:
        project = (await _claimed(conn, user, run_id))[4]
        uploads = tables.blob_uploads
        query = select(uploads.c.kind).where(uploads.c.upload_id.in_([UUID(upload_id) for upload_id in ids]))
        kinds = (await conn.execute(query)).scalars().all()
    if len(set(kinds)) != len(kinds):
        raise HTTPException(422, "a run has one log and one diff: commit one upload of each kind")
    try:
        committed = await commit_uploads(request, user, project, ids, kinds=frozenset(RUN_BLOB_KINDS))
    except Mismatch as exc:
        message = (
            f"{len(exc.problems)} upload(s) do not match what was declared, such as {exc.problems[0]['upload_id']}: "
            f"{exc.problems[0]['problem']}. Nothing was committed and the uploads were discarded; ask for new ones"
        )
        return error_response(request, 422, message, detail=exc.problems)
    found = {BLOB_COLUMNS[blob.kind]: blob.sha256 for blob in committed.blobs}
    r = tables.runs
    record = (
        update(r)
        .values(
            log_sha256=func.coalesce(found.get("log_sha256"), r.c.log_sha256),
            diff_sha256=func.coalesce(found.get("diff_sha256"), r.c.diff_sha256),
        )
        .where(r.c.id == run_id)
        .returning(r.c.log_sha256, r.c.diff_sha256)
    )
    async with request.app.state.engine.begin() as conn:
        await _claimed(conn, user, run_id)  # the worker may have been revoked while the bytes were read
        row = (await conn.execute(record)).one()
    log.info("run blobs recorded", extra={"run_id": run_id, "kinds": sorted(blob.kind for blob in committed.blobs)})
    return RunBlobs(**row._mapping)


# The readers' side


def _events_after(run_id: int, after: int, kinds: list[str], limit: int):
    """The events of run ``run_id`` after seq ``after``, of ``kinds`` (any kind when empty), in seq order."""
    e = tables.run_events
    query = select(e.c.seq, e.c.at, e.c.kind, e.c.body, e.c.truncated).where(e.c.run_id == run_id, e.c.seq > after)
    if kinds:
        query = query.where(e.c.kind.in_(kinds))
    return query.order_by(e.c.seq).limit(limit)


async def _progress(conn: AsyncConnection, run_id: int):
    """(state, event_seq) of run ``run_id``."""
    r = tables.runs
    return (await conn.execute(select(r.c.state, r.c.event_seq).where(r.c.id == run_id))).one()


def _events(rows) -> list[RunEvent]:
    return [RunEvent(**row._mapping) for row in rows]


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
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> RunEvents:
    """The run's events after ``after``, in seq order."""
    query = _events_after(run_id, after, list(dict.fromkeys(kind)), limit + 1)
    async with request.app.state.engine.begin() as conn:
        await readable_run(conn, user, project, run_id, sink)
        state, last_seq = await _progress(conn, run_id)
        rows = (await conn.execute(query)).all()
    events = _events(rows[:limit])
    return RunEvents(run_id=run_id, state=state, last_seq=last_seq, events=events, more=len(rows) > limit)


async def _streamed_run(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> int:
    """The run a stream follows, checked before the stream starts so a refusal is an ordinary error answer."""
    async with request.app.state.engine.begin() as conn:
        await readable_run(conn, user, project, run_id, sink)
    return run_id


@stream_router.get(
    "/{project}/runs/{run_id}/stream",
    response_class=EventSourceResponse,
    responses={403: {"description": "Forbidden", **JSON_ERROR}, 404: {"description": "Not Found", **JSON_ERROR}},
)
async def stream(
    request: Request,
    followed: Annotated[int, Depends(_streamed_run)],
    last_event_id: Annotated[
        int | None, Header(alias="Last-Event-ID", ge=0, le=MAX_SEQ, description="sent by a reconnecting client")
    ] = None,
    after: Annotated[int, Query(ge=0, le=MAX_SEQ, description="where a first connection starts")] = 0,
) -> AsyncIterable[ServerSentEvent]:
    """The run's events as server-sent events, each with its seq as id, then an ``end`` event once the run is final
    and every event was sent; a ``: ping`` comment keeps an idle stream open."""
    streams: RunStreams = request.app.state.run_streams
    streams.start()
    engine = request.app.state.engine
    seen = last_event_id if last_event_id is not None else after
    with streams.subscribe(followed) as wakeup:
        while True:
            async with engine.begin() as conn:
                # The state first: when it is final, the move's own event is committed already and read below.
                state, _ = await _progress(conn, followed)
                rows = (await conn.execute(_events_after(followed, seen, [], STREAM_BATCH))).all()
            for event in _events(rows):
                seen = event.seq
                yield ServerSentEvent(data=event, id=str(event.seq))
            if len(rows) == STREAM_BATCH:
                continue
            if state in runs.TERMINAL_STATES:
                yield ServerSentEvent(event="end", data={"state": state, "last_seq": seen})
                return
            await wakeup.wait(STREAM_POLL_SECONDS)


# The owner's messages


async def _chat_ended(conn: AsyncConnection, run_id: int) -> bool:
    """Whether the owner ended the chat of author run ``run_id``, which its worker holds still."""
    r = tables.runs
    query = select(r.c.finish_requested_at.is_not(None)).where(r.c.id == run_id)
    return bool((await conn.execute(query)).scalar_one())


async def write_user_message(conn: AsyncConnection, run_id: int, body: dict) -> int:
    """Write the ``user_message`` event of a message of the owner into the log of run ``run_id``, whose row the
    caller holds locked, and wake its streams; its seq."""
    seq = await next_seq(conn, run_id)
    await conn.execute(insert(tables.run_events).values(run_id=run_id, seq=seq, kind="user_message", body=body))
    await notify_events(conn, run_id)
    return seq


@router.post(
    "/{project}/runs/{run_id}/messages",
    status_code=201,
    response_model=Message,
    responses={**REFUSALS, 413: {"model": ErrorBody}},
)
async def send_message(request: Request, project: ProjectName, run_id: RunId, body: MessageIn, user: CurrentUser):
    """Leave a message for the run's agent in its inbox, which the worker hands to the agent; a reply to a parked
    author run queues the run that resumes it, whose inbox takes the message."""
    async with request.app.state.engine.begin() as conn:
        access, row = await _owned_run(conn, user, project, run_id, "send a message to")
        state, plan_id, key, kind = row[2], row[3], row[4], row[11]
        resumes = kind == "author" and state == "parked"
        if state not in runs.MESSAGE_STATES and not resumes:
            if kind == "author":
                raise HTTPException(
                    409, f"run {run_id} is {state}: its chat is over; dispatch a new author run to go on"
                )
            raise HTTPException(
                409, f"run {run_id} is {state}: a message goes to a run that is queued or held by its worker"
            )
        if kind == "author" and await _chat_ended(conn, run_id):
            raise HTTPException(
                409, f"run {run_id}: you ended its chat, and the run ends done once its worker hears of it"
            )
        await web_only_steering(conn, user, run_id, "send its agent a message", "send it from the web", "sent")
        _, last_seq = await _progress(conn, run_id)
        if last_seq >= runs.MAX_RUN_EVENTS:
            message = RUN_FULL.format(id=run_id, count=last_seq, limit=runs.MAX_RUN_EVENTS)
            return error_response(request, 413, message, detail=[{"limit": "events_per_run"}])
        inbox_run = run_id
        if resumes:
            from evo_agents.hub.server.decisions import resume_parked  # it reads runs through this module

            inbox_run = await resume_parked(conn, user, run_id, f"as {user.login} replied in its chat")
        inbox = tables.run_inbox
        left = (
            insert(inbox)
            .values(run_id=inbox_run, sent_by=user.user_id, body=body.text)
            .returning(inbox.c.id, inbox.c.created_at)
        )
        message_id, created_at = (await conn.execute(left)).one()
        event = {"text": body.text, "from": user.login, "message_id": message_id}
        seq = await write_user_message(conn, inbox_run, event)
        target = f"{_run_target(project, plan_id, key, run_id)} message:{message_id}"
        if inbox_run != run_id:
            target += f" resumed as run:{inbox_run}"
        await _audit_run(conn, user, access, audit.RUN_MESSAGE, target)
    log.info(
        "run message sent",
        extra={"run_id": run_id, "inbox_run": inbox_run, "message_id": message_id, "login": user.login},
    )
    return Message(
        id=message_id,
        run_id=inbox_run,
        seq=seq,
        text=body.text,
        sent_by=user.login,
        created_at=created_at,
        delivered_at=None,
    )


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
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> DiffLink:
    """A presigned GET of the diff the run's worker uploaded when the run ended."""
    store = blob_store(request)
    async with request.app.state.engine.begin() as conn:
        await readable_run(conn, user, project, run_id, sink)
        r, b = tables.runs, tables.blobs
        query = (
            select(r.c.diff_sha256, b.c.size)
            .select_from(r.outerjoin(b, and_(b.c.project_id == r.c.project_id, b.c.sha256 == r.c.diff_sha256)))
            .where(r.c.id == run_id)
        )
        sha256, size = (await conn.execute(query)).one()
    if sha256 is None:
        raise HTTPException(404, f"run {run_id} has no diff: its worker uploads one when the run ends")
    expires_at = datetime.now(UTC) + GET_TTL  # taken before signing, so never later than the URL
    filename = f"run-{run_id}.diff" if download else None
    url = await asyncio.to_thread(store.presign_get, sha256, filename=filename)
    return DiffLink(sha256=sha256, size=size, url=url, expires_at=expires_at)
