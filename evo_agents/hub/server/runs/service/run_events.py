"""A run's log, its live stream, the owner's messages to its agent, and the log and diff its worker uploads
(``evo_agents.hub.server.run_events`` has the protocol): who may do what, and the transactions, opened with
UnitOfWork."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import HTTPException
from fastapi.sse import ServerSentEvent
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import chat_runs, runs, spend_store
from evo_agents.hub.server import audit
from evo_agents.hub.server.agent_accounts.service.mask import masked, run_values
from evo_agents.hub.server.attachments.service import messages as attachment_store
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.chats.repository.store import wake_run_chat
from evo_agents.hub.server.run_state import next_seq, notify_events
from evo_agents.hub.server.runs.models import NOT_HELD
from evo_agents.hub.server.runs.models.attachment_parts import as_event
from evo_agents.hub.server.runs.models.run_events import (
    BLOB_COLUMNS,
    RUN_FULL,
    STREAM_BATCH,
    EventIn,
    EventsAck,
    Inbox,
    InboxMessage,
    Message,
    MessageIn,
    RunBlobs,
    RunEvent,
    RunEvents,
    RunFull,
)
from evo_agents.hub.server.runs.repository import run_events as repository
from evo_agents.hub.server.runs.service.audits import audit_run, run_target
from evo_agents.hub.server.runs.service.controls import owned_run, web_only_steering
from evo_agents.hub.server.runs.service.views import readable_run
from evo_agents.hub.server.security import Principal, still_live

log = logging.getLogger("evo_agents.hub.server.run_events")

RecordTools = Callable[[AsyncConnection, int], Awaitable[object]]  # tool_stats.record, which the route passes


# The worker's side


async def claimed_run(conn: AsyncConnection, user: Principal, run_id: int):
    """(worker name, state, event_seq, events_acked, project) of run ``run_id``, its row locked, when the worker of
    ``user`` claimed it; 403 for a token of no live worker, 404 for another worker's run or none."""
    worker = await repository.live_worker(conn, user.token_id)
    if worker is None:
        raise HTTPException(403, "this worker token belongs to no live worker: join the machine again")
    row = await repository.locked_run(conn, run_id)
    if row is None or row.worker_id != worker.id:
        raise HTTPException(404, f"this worker did not claim run {run_id}")
    return (worker.name, row.state, row.event_seq, row.events_acked, row.project)


def fresh_events(events: list[EventIn], acked: int) -> list[EventIn]:
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


async def _event_rows(conn: AsyncConnection, sealer, run_id: int, first: int, fresh: list[EventIn]) -> list[dict]:
    leased = await run_values(conn, sealer, run_id)  # the floor S3, whatever the daemon did
    rows = []
    for number, event in enumerate(fresh, start=first):
        stored, cut = runs.fit_event_body(runs.without_nul(masked(event.body, leased)))
        rows.append(
            {"run_id": run_id, "seq": number, "seen_at": event.at, "kind": event.kind, "body": stored, "truncated": cut}
        )
    return rows


async def store_events(
    conn: AsyncConnection,
    sealer,
    run_id: int,
    state: str,
    last_seq: int,
    acked: int,
    events: list[EventIn],
    record_tools: RecordTools,
) -> EventsAck | RunFull:
    """Store the events of a batch that follow the ``acked`` ones of run ``run_id`` (in ``state``, its row locked by
    the caller, ``last_seq`` the hub's last number), in the caller's transaction: an ``EventsAck``, or ``RunFull`` for
    a run that would pass its limit. ``record_tools`` writes the run's tool figures again (``tool_stats.record``)."""
    fresh = fresh_events(events, acked)
    if not fresh:
        return EventsAck(ack_seq=acked, stored=0)
    if last_seq + len(fresh) > runs.MAX_RUN_EVENTS:
        message = RUN_FULL.format(id=run_id, count=last_seq, limit=runs.MAX_RUN_EVENTS)
        return RunFull(message, [{"limit": "events_per_run", "max": runs.MAX_RUN_EVENTS, "ack_seq": acked}])
    rows = await _event_rows(conn, sealer, run_id, last_seq + 1, fresh)
    await repository.add_events(conn, run_id, rows)
    if state in runs.TERMINAL_STATES:  # the spool's last events, after the end: the figures take them in
        await record_tools(conn, run_id)
    if any(event.kind == "usage_update" for event in fresh):  # what the run spent so far, whatever its state
        await spend_store.record_safely(conn, run_id)
    if any(event.kind == "system" and chat_runs.SETUP_FIELD in event.body for event in fresh):
        await wake_run_chat(conn, run_id)  # a step of a chat run's setup: its thread shows it (chat_progress)
    await notify_events(conn, run_id)
    cut = sum(1 for row in rows if row["truncated"])
    log.debug("run events stored", extra={"run_id": run_id, "stored": len(fresh), "truncated": cut})
    return EventsAck(ack_seq=acked + len(fresh), stored=len(fresh))


async def post_events(
    engine: AsyncEngine, sealer, user: Principal, run_id: int, events: list[EventIn], record_tools: RecordTools
) -> EventsAck | RunFull:
    """Store the events of a batch from the worker that claimed run ``run_id``."""
    async with UnitOfWork(engine) as conn:
        _, state, last_seq, acked, _ = await claimed_run(conn, user, run_id)
        return await store_events(conn, sealer, run_id, state, last_seq, acked, events, record_tools)


async def take_inbox(engine: AsyncEngine, user: Principal, run_id: int, ack: int | None) -> Inbox:
    """Mark the messages up to ``ack`` delivered, and hand out the ones that still wait, for a run the worker holds."""
    async with UnitOfWork(engine) as conn:
        _, state, *_ = await claimed_run(conn, user, run_id)
        if state not in runs.HELD_STATES:
            raise HTTPException(404, NOT_HELD.format(id=run_id))
        if ack is not None:
            await repository.mark_delivered(conn, run_id, ack)
            await wake_run_chat(conn, run_id)  # a chat message the agent took leaves step inbox (chat_progress)
        rows = await repository.waiting_messages(conn, run_id)
        files = await attachment_store.of_inbox(conn, [row.id for row in rows])
    return Inbox(messages=[InboxMessage(**row._mapping, attachments=files.get(row.id, [])) for row in rows])


async def claimed_project(engine: AsyncEngine, user: Principal, run_id: int) -> str:
    """The project of run ``run_id``, which the worker of ``user`` claimed."""
    async with UnitOfWork(engine) as conn:
        return (await claimed_run(conn, user, run_id))[4]


async def committed_kinds(engine: AsyncEngine, user: Principal, run_id: int, upload_ids: list[str]):
    """(project, kinds of ``upload_ids``) of run ``run_id``, which the worker of ``user`` claimed; 422 when a kind
    comes twice."""
    async with UnitOfWork(engine) as conn:
        project = (await claimed_run(conn, user, run_id))[4]
        kinds = await repository.upload_kinds(conn, upload_ids)
    if len(set(kinds)) != len(kinds):
        raise HTTPException(422, "a run has one log and one diff: commit one upload of each kind")
    return project, kinds


async def record_blobs(engine: AsyncEngine, user: Principal, run_id: int, blobs) -> RunBlobs:
    """Record the committed ``blobs`` (each with its kind and sha256) on run ``run_id``."""
    found = {BLOB_COLUMNS[blob.kind]: blob.sha256 for blob in blobs}
    async with UnitOfWork(engine) as conn:
        await claimed_run(conn, user, run_id)  # the worker may have been revoked while the bytes were read
        row = await repository.record_blobs(conn, run_id, found)
    log.info("run blobs recorded", extra={"run_id": run_id, "kinds": sorted(blob.kind for blob in blobs)})
    return RunBlobs(**row._mapping)


# The readers' side


def as_events(rows) -> list[RunEvent]:
    return [RunEvent(**row._mapping) for row in rows]


async def list_events(
    engine: AsyncEngine,
    user: Principal,
    project: str,
    run_id: int,
    *,
    after: int,
    kinds: list[str],
    limit: int,
    sink: str | None,
) -> RunEvents:
    """The run's events after ``after``, in seq order."""
    async with UnitOfWork(engine) as conn:
        await readable_run(conn, user, project, run_id, sink)
        state, last_seq = await repository.progress(conn, run_id)
        rows = await repository.events_after(conn, run_id, after, kinds, limit + 1)
    events = as_events(rows[:limit])
    return RunEvents(run_id=run_id, state=state, last_seq=last_seq, events=events, more=len(rows) > limit)


async def check_readable(engine: AsyncEngine, user: Principal, project: str, run_id: int, sink: str | None) -> None:
    async with UnitOfWork(engine) as conn:
        await readable_run(conn, user, project, run_id, sink)


async def follow(
    engine: AsyncEngine, streams, user: Principal, run_id: int, seen: int, poll: float
) -> AsyncIterator[ServerSentEvent]:
    """The events of run ``run_id`` after ``seen`` as server-sent events, then ``end`` once the run is final and every
    event was sent; it waits on ``streams`` (``RunStreams``) at most ``poll`` seconds, and stops once ``user`` no
    longer holds."""
    with streams.subscribe(run_id) as wakeup:
        while True:
            async with UnitOfWork(engine) as conn:
                if not await still_live(conn, user):
                    return
                # The state first: when it is final, the move's own event is committed already and read below.
                state, _ = await repository.progress(conn, run_id)
                rows = await repository.events_after(conn, run_id, seen, [], STREAM_BATCH)
            for event in as_events(rows):
                seen = event.seq
                yield ServerSentEvent(data=event, id=str(event.seq))
            if len(rows) == STREAM_BATCH:
                continue
            if state in runs.TERMINAL_STATES or state == "ended":  # ended: a local run's end
                yield ServerSentEvent(event="end", data={"state": state, "last_seq": seen})
                return
            await wakeup.wait(poll)


# The owner's messages


async def write_user_message(conn: AsyncConnection, run_id: int, body: dict) -> int:
    """Write the ``user_message`` event of a message of the owner into the log of run ``run_id``, whose row the
    caller holds locked, and wake its streams; its seq."""
    seq = await next_seq(conn, run_id)
    await repository.add_user_message(conn, run_id, seq, runs.without_nul(body))  # pyright: ignore[reportArgumentType]
    await notify_events(conn, run_id)
    return seq


async def _check_messageable(conn: AsyncConnection, run_id: int, state: str, kind: str, resumes: bool) -> None:
    if state not in runs.MESSAGE_STATES and not resumes:
        if kind == "author":
            raise HTTPException(409, f"run {run_id} is {state}: its chat is over; dispatch a new author run to go on")
        raise HTTPException(
            409, f"run {run_id} is {state}: a message goes to a run that is queued or held by its worker"
        )
    if kind == "author" and await repository.chat_ended(conn, run_id):
        raise HTTPException(409, f"run {run_id}: you ended its chat, and the run ends done once its worker hears of it")


async def _leave(conn: AsyncConnection, user: Principal, run_id: int, body: MessageIn, resumes: bool):
    """Leave the message in the inbox of run ``run_id``, or of the run that resumes it; (inbox run, id, created_at,
    seq, files)."""
    files = await attachment_store.take(conn, body.attachment_ids, user.user_id, run_id=run_id)
    inbox_run = run_id
    if resumes:
        # it moves runs through this module
        from evo_agents.hub.server.decisions.service.resume import resume_parked

        why = f"as {user.login} replied in its chat"
        inbox_run = await resume_parked(conn, user, run_id, why, woken_by=body.text)
    message_id, created_at = await repository.add_to_inbox(conn, inbox_run, user.user_id, body.text)
    await attachment_store.hold_in_inbox(conn, message_id, files)
    event = {"text": body.text, "from": user.login, "message_id": message_id}
    seq = await write_user_message(conn, inbox_run, event | ({"attachments": as_event(files)} if files else {}))
    return inbox_run, message_id, created_at, seq, files


async def send_message(
    engine: AsyncEngine, user: Principal, project: str, run_id: int, body: MessageIn
) -> Message | RunFull:
    """Leave a message for the run's agent in its inbox; a reply to a parked author run queues the run that resumes
    it, whose inbox takes the message."""
    async with UnitOfWork(engine) as conn:
        access, row = await owned_run(conn, user, project, run_id, "send a message to")
        state, plan_id, key, kind = row[2], row[3], row[4], row[11]
        resumes = kind in ("author", "chat") and state == "parked"
        await _check_messageable(conn, run_id, state, kind, resumes)
        await web_only_steering(conn, user, run_id, "send its agent a message", "send it from the web", "sent")
        _, last_seq = await repository.progress(conn, run_id)
        if last_seq >= runs.MAX_RUN_EVENTS:
            message = RUN_FULL.format(id=run_id, count=last_seq, limit=runs.MAX_RUN_EVENTS)
            return RunFull(message, [{"limit": "events_per_run"}])
        inbox_run, message_id, created_at, seq, files = await _leave(conn, user, run_id, body, resumes)
        target = f"{run_target(project, plan_id, key, run_id)} message:{message_id}"
        if inbox_run != run_id:
            target += f" resumed as run:{inbox_run}"
        await audit_run(conn, user, access, audit.RUN_MESSAGE, target)
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
        attachments=files,
    )


# The diff


async def diff_of(engine: AsyncEngine, user: Principal, project: str, run_id: int, sink: str | None):
    """(sha256, size) of the diff of a run the user reads; 404 when its worker uploaded none."""
    async with UnitOfWork(engine) as conn:
        await readable_run(conn, user, project, run_id, sink)
        sha256, size = await repository.diff_of(conn, run_id)
    if sha256 is None:
        raise HTTPException(404, f"run {run_id} has no diff: its worker uploads one when the run ends")
    return sha256, size
