"""The models of notifications: the limits and the reasons the outbox writes, the notification as a channel's class
sends it (Outgoing) and the delivery it prepares for (Sending), the exceptions a class raises (RetryAfter,
Undeliverable, ChannelGone), a member's channel (Found), and the shapes of the API: a worker's notice (NoticeIn), a
notification as a member reads it (Notification) and the list, the count and the read of them, and a hub admin's link
of a member's channel. They import no other layer of the package."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import Path
from pydantic import BaseModel, Field, model_validator

from evo_agents.hub import review, runs
from evo_agents.hub.server.ops_views import OpsHolding
from evo_agents.hub.server.output_views import RunOutput
from evo_agents.hub.server.runs.models import LINE, MAX_ID, OBJECT_NAME
from evo_agents.plugins.manifest import CHANNEL_KIND

# The outbox

WEB = "web"  # the channel every member has, without a row: a delivery whose channel_id is NULL
# The tokens.kind of a web session, security.WEB: a copy, as security imports the routes of runs and, through them,
# the service of notifications (tests/hub/test_notifications.py checks the two are the same)
WEB_SESSION = "web"
TITLE_CHARS = 200  # notifications.title, as schema 0010 bounds it
MAX_ERROR_CHARS = 2000  # notification_deliveries.last_error
MAX_EXTERNAL_ID_CHARS = 200  # notification_deliveries.external_id, as schema 0015 bounds it
DELIVERY_BATCH = 500  # deliveries one pass of the job takes
SEND_TIMEOUT_SECONDS = 30.0  # a channel's send that takes longer counts as failed
BACKOFF_SECONDS = 60  # after the first failed try; doubled after each next one
BACKOFF_MAX_SECONDS = 3600

NO_GRANT = "the member holds no grant on the notification's project any more"
SESSION_ENDED = "the web session that linked this channel ended (signed out, revoked or expired): link it again"
DECISION_CLOSED = "the decision was answered or closed before its push went out"
DECISION_WINDOW = timedelta(minutes=30)  # a channel's delivery of a decision, or its edit, is tried this long
EDIT_BATCH = 500  # first deliveries one sweep of queue_edits gives an edit
# Why a message went without its notification's content: notification_deliveries.withheld (0265)
WITHHELD = ("label", "default_label", "project")

# A member's list

MAX_LIST = 200
COUNT_TTL = 60.0  # seconds the cache keeps a member's count; a write of what it is read from drops it before
MAX_OFFSET = 100_000
MAX_READ_IDS = 500
MAX_QUERY_CHARS = 200  # the words GET /v1/me/notifications searches the titles and bodies for

# The members' channels

MAX_CONFIG_BYTES = 16 * 1024  # a channel's config, or its state, as JSON
RESERVED_KEYS = ("disabled_reason", "disabled_at", "state")  # what the hub, not a link, writes into a channel's config
STATE = "state"
NOT_YOURS = "the hub has nothing by that for you to answer: it is not of a project you hold a grant on, or not there"
RESTRICTED_ANSWER = "answer this on the hub: its project keeps its decisions and proposals off this channel"
FROM_SESSION = (
    "a channel is linked from a web session only, so a token that leaked cannot link one of its own: sign in on the "
    "web and link it there"
)
NO_USER = "the hub has no user {login}"
LABELLED_ANSWER = "answer this on the hub: the label {label} of decision {decision_id} keeps it off this channel"
ANSWERABLE = ("decision", "proposal")
# Why a message goes without its notification's content (``extra["restricted_by"]``, notification_deliveries.withheld)
BY_LABEL = "label"  # the decision's label sits above the clearance of the channel's sink, or of its plugin
BY_DEFAULT_LABEL = "default_label"  # a decision without a label: its project keeps it off, by the hub sink as before
BY_PROJECT = "project"  # a proposal or a notice its project keeps off, or a decision or proposal of no project


@dataclass(frozen=True)
class Sending:
    """The delivery a channel's ``prepare`` prepares for (SENDING): its id, the kind of its channel, and for an edit
    the delivery whose message it edits and that message's id."""

    delivery_id: int
    channel: str
    edit_of: int | None = None
    edit_external_id: str | None = None


@dataclass(frozen=True)
class Outgoing:
    """One notification as a channel's class sends it."""

    id: int
    user_id: int
    login: str
    kind: str
    notice_kind: str | None
    project: str | None
    run_id: int | None
    decision_id: int | None
    title: str
    body: str | None
    details: dict | None
    link: str | None
    created_at: datetime
    proposal_id: int | None = None
    extra: dict | None = None  # what a channel's ``prepare`` read besides the notification


OUTGOING = tuple(field.name for field in dataclasses.fields(Outgoing) if field.name != "extra")


class RetryAfter(Exception):  # noqa: N818 (what the service asked for, not an error of the hub)
    """The channel's service asked to wait ``seconds`` before the next try, which is not a failed try."""

    def __init__(self, seconds: float, message: str):
        super().__init__(message)
        self.seconds = max(1.0, min(float(seconds), BACKOFF_MAX_SECONDS))


class Undeliverable(Exception):  # noqa: N818
    """The notification can never go out on this channel: its delivery fails at once."""


class ChannelGone(Undeliverable):
    """The channel no longer reaches its member, as when they blocked the bot: the delivery fails and the channel is
    turned off, with the message as its ``disabled_reason``."""


@dataclass(frozen=True)
class Found:
    """A member's channel: who it speaks for, its id and its config as stored."""

    user_id: int
    login: str
    channel_id: int
    config: dict


# The API


class NoticeIn(BaseModel):
    kind: Literal[runs.WORKER_NOTICE_KINDS]  # pyright: ignore[reportInvalidTypeForm]
    title: str = Field(min_length=1, max_length=TITLE_CHARS, pattern=LINE)
    body: str | None = Field(
        None, min_length=1, max_length=runs.MAX_NOTICE_BODY_BYTES, description="at most 16 KiB of UTF-8"
    )
    repo: str | None = Field(None, min_length=1, max_length=200, pattern=LINE, description="one of the run's repos")
    branch: str | None = Field(None, min_length=1, max_length=255, pattern=LINE)
    commits: list[Annotated[str, Field(pattern=OBJECT_NAME)]] = Field(
        default_factory=list, max_length=runs.MAX_NOTICE_COMMITS, description="the commits pushed or merged"
    )

    @model_validator(mode="after")
    def _bounded(self):
        if self.body is not None and len(self.body.encode()) > runs.MAX_NOTICE_BODY_BYTES:
            raise ValueError(f"a notice's body is at most {runs.MAX_NOTICE_BODY_BYTES} bytes of UTF-8")
        return self

    def details(self) -> dict | None:
        found = {"repo": self.repo, "branch": self.branch, "commits": list(self.commits) or None}
        kept = {key: value for key, value in found.items() if value is not None}
        return kept or None


class Notification(BaseModel):
    id: int
    kind: Literal[runs.NOTIFICATION_KINDS]  # pyright: ignore[reportInvalidTypeForm]
    notice_kind: str | None = Field(
        description="a notice's kind: one of the hub's, or one a core plugin declares (contributes.notice_kinds, its "
        "table prefix first); null for a decision"
    )
    project: str | None
    run_id: int | None
    decision_id: int | None
    decision_state: Literal[runs.DECISION_STATES] | None = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="the state of the decision now, for a notification of kind decision"
    )
    proposal_id: int | None = Field(None, description="the proposal, for a notification of kind proposal")
    proposal_state: Literal[review.PROPOSAL_STATES] | None = Field(  # pyright: ignore[reportInvalidTypeForm]
        None, description="the state of the proposal now, for a notification of kind proposal"
    )
    title: str
    body: str | None
    details: dict | None = Field(description="a notice's facts, such as the repo, branch and commits of a push")
    link: str | None = Field(description="the hub web's page that shows it, a path of the hub's own site")
    created_at: datetime
    read_at: datetime | None
    report: bool = Field(
        False,
        description="a notice that only reports (a push or merge into a default branch, a plan finished, a run's files, "
        "asking nothing): stored read, sent to no channel (nor a device, unless you chose the interrupt tier for its "
        "kind), and counted in Today's summary",
    )
    outputs: list[RunOutput] = Field(
        default_factory=list,
        description="the files it carries, oldest first: a decision's, or a notice run_outputs' (an output deleted "
        "since is left out), each with the URL its reader fetches the bytes from",
    )
    ops: OpsHolding | None = Field(
        None,
        description="the batch of the ops queue whose Orchestrator (the project's default system's) holds it, with "
        "its thread; null when it waits on a person",
    )


def notifications_of(rows) -> list[Notification]:
    return [Notification(**row._mapping) for row in rows]


class NotificationList(BaseModel):
    notifications: list[Notification] = Field(description="open decisions and proposals first, then newest first")
    total: int = Field(description="notifications that match the filters")
    limit: int
    offset: int


class NotificationCount(BaseModel):
    unread: int = Field(description="notifications not read yet: the bell's number")
    open_decisions: int = Field(description="decisions that still wait for the member's answer, read or not")
    open_proposals: int = Field(0, description="proposals in the member's Inbox that wait for an answer, read or not")


class ReadRequest(BaseModel):
    ids: list[Annotated[int, Field(ge=1, le=MAX_ID)]] | None = Field(
        None, min_length=1, max_length=MAX_READ_IDS, description="the notifications to mark read"
    )
    all: bool = Field(False, description="mark every notification of the caller read")

    @model_validator(mode="after")
    def _one(self):
        if (self.ids is None) == (not self.all):
            raise ValueError("name the notifications to mark read with ids, or set all, not both")
        return self


class ReadResult(BaseModel):
    read: int = Field(description="notifications this request marked read")
    unread: int = Field(description="notifications left unread")


ChannelKind = Annotated[str, Path(pattern=CHANNEL_KIND)]


class ChannelLink(BaseModel):
    config: dict[str, Any] = Field(
        description="the channel's config as its plugin keeps it (the ids of the member's chat), at most 16 KiB of JSON"
    )
    unique_on: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="keys of config that name one chat: another member's channel of the kind with the same values "
        "there goes, so a chat speaks for one member",
    )


class ChannelLinked(BaseModel):
    login: str
    kind: str
    config: dict[str, Any]
    linked_by: str = Field(description="the hub admin who linked it")


class ChannelUnlinked(BaseModel):
    login: str
    kind: str
    unlinked: bool = Field(description="whether the member had a channel of the kind")
