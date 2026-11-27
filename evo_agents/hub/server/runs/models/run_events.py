"""The shapes and limits of a run's log, its stream, the owner's messages to its agent, and the log and diff its worker
uploads (``evo_agents.hub.server.run_events``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import UUID4, AwareDatetime, BaseModel, Field, model_validator

from evo_agents.hub import runs
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.runs.models.attachment_parts import Attachment, AttachmentIds

MAX_SEQ = 2**31 - 1  # run_events.seq and runs.events_acked are integers
STREAM_BATCH = 500  # events a stream reads at a time
MAX_EVENTS_PAGE = 1000
MAX_INBOX = 100  # messages one POST of the worker's inbox hands out
RUN_BLOB_KINDS = ("run-log", "run-diff")
BLOB_COLUMNS = {"run-log": "log_sha256", "run-diff": "diff_sha256"}
BATCH_TOO_LARGE = f"a batch of events is at most {runs.MAX_BATCH_BYTES} bytes of request; send fewer at a time"
RUN_FULL = (
    "run {id} has {count} events, and a run keeps at most {limit} of them: nothing of this batch was stored, and "
    "the worker should send no more of this run's events"
)


@dataclass(frozen=True)
class RunFull:
    """A refusal of a run past its MAX_RUN_EVENTS: the route answers it 413 with this message and detail."""

    message: str
    detail: list[dict] = field(default_factory=list)


class EventIn(BaseModel):
    seq: int = Field(ge=1, le=MAX_SEQ, description="the worker's number of the event: 1 for the run's first")
    at: AwareDatetime = Field(description="when the worker saw it; a time after the hub's now is taken as now")
    kind: Literal[runs.WORKER_EVENT_KINDS] = Field(description="user_message and state are the hub's own")  # pyright: ignore[reportInvalidTypeForm]
    body: dict[str, Any] = Field(description="a JSON object; over 64 KiB of JSON, the hub cuts it")


class EventBatch(BaseModel):
    events: list[EventIn] = Field(max_length=runs.MAX_BATCH_EVENTS)


class EventsAck(BaseModel):
    ack_seq: int = Field(description="the worker's highest seq stored with none missing below: drop the spool to it")
    stored: int = Field(description="events of this batch stored now; the others were resends or after a gap")


class RunEvent(BaseModel):
    seq: int = Field(description="the hub's number of the event in the run: events?after= and Last-Event-ID take it")
    at: datetime
    kind: Literal[runs.EVENT_KINDS]  # pyright: ignore[reportInvalidTypeForm]
    body: dict[str, Any]
    truncated: bool = Field(description="the hub cut the body to 64 KiB of JSON")


class RunEvents(BaseModel):
    run_id: int
    state: Literal[runs.SHOWN_STATES]  # pyright: ignore[reportInvalidTypeForm]
    last_seq: int = Field(description="the seq of the run's latest event")
    events: list[RunEvent] = Field(description="in seq order")
    more: bool = Field(description="more events follow the last one here: ask again after it")


class MessageIn(BaseModel):
    text: str = Field(
        max_length=runs.MAX_MESSAGE_BYTES, description="at most 8 KiB of UTF-8; empty only with attachment_ids"
    )
    attachment_ids: AttachmentIds = Field(default_factory=list)

    @model_validator(mode="after")
    def _bounded(self):
        if not self.text.strip() and not self.attachment_ids:
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
    attachments: list[Attachment] = Field(default_factory=list, description="the files it carries")


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
    attachments: list[Attachment] = Field(
        default_factory=list,
        description="the files it carries, whose bytes GET /v1/worker/runs/{id}/attachments/{aid}/content serves",
    )


class Inbox(BaseModel):
    messages: list[InboxMessage] = Field(description="the messages not delivered yet, oldest first")


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
