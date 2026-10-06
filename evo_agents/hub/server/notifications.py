"""Notifications: the outbox every decision and notice of a plan run goes through, the channels that deliver them, and
the routes a member reads them with. ``docs/notifications.md`` is the design and ``runs`` holds the kinds and limits.

``notify`` stores, in the caller's transaction, one notification for one member and one delivery of it for each
channel of the member's that is on: the web, which every member has without a row of notification_channels (a
delivery without a channel), and each enabled row. The table is the outbox: nothing is sent while the transaction is
open, and a hub that stops between the commit and the send loses nothing.

``deliver_notifications`` is the job hub.deliver_notifications, every minute. It takes the pending deliveries whose
``next_at`` has passed, each in a transaction of its own with ``FOR UPDATE SKIP LOCKED``, and hands each to an
instance of the class CHANNELS names for its channel's kind (WEB for a delivery without a channel). The web's class
sends nothing, since the web reads notifications from the table, so its deliveries are delivered at once. A class
that raises leaves the delivery pending with ``last_error`` and a ``next_at`` BACKOFF_SECONDS later, doubling from one
try to the next up to BACKOFF_MAX_SECONDS, and its MAX_DELIVERY_ATTEMPTS-th failure marks it failed. A kind with no
class in CHANNELS, or a channel turned off since, fails its delivery at once with that reason, so a hub that drops a
channel does not try it forever. Adding a channel takes a subclass of Channel and its entry in CHANNELS; the tables,
the outbox and the job stay as they are.

POST /v1/worker/runs/{id}/notices takes a notice (``runs.NOTICE_KINDS``) from the worker holding a plan run: a title,
a body, and the repo, branch and commits of a push or merge. It notifies the run's owner, links to the run's page and
leaves a ``system`` event in the run's log. A run of one step sends no notice (404, as any run the worker does not
hold). The hub sends ``plan_finished`` and ``run_failed`` itself when a plan run ends (``run_state``).

A member reads their own notifications: GET /v1/me/notifications (open decisions first, then newest first; filtered
by unread, kind and project, a page at a time), GET /v1/me/notifications/count (the unread ones and the decisions
still waiting for an answer, for the bell), and POST /v1/me/notifications/read (by ids, or all), audited as
notification.read with the ids, never the text. A notification of a project the member no longer holds a grant on is
not shown. Answering a decision reads its notification too (``decisions``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, model_validator

from evo_agents.hub import runs
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs import LINE, MAX_ID, OBJECT_NAME, REFUSALS, RunId, _held_plan_run
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

WEB = "web"  # the channel every member has, without a row: a delivery whose channel_id is NULL
TITLE_CHARS = 200  # notifications.title, as schema 0010 bounds it
MAX_ERROR_CHARS = 2000  # notification_deliveries.last_error
DELIVERY_BATCH = 500  # deliveries one pass of the job takes
SEND_TIMEOUT_SECONDS = 30.0  # a channel's send that takes longer counts as failed
BACKOFF_SECONDS = 60  # after the first failed try; doubled after each next one
BACKOFF_MAX_SECONDS = 3600
MAX_LIST = 200
MAX_OFFSET = 100_000
MAX_READ_IDS = 500

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
router = APIRouter(prefix="/v1/me", tags=["notifications"], responses={401: {"model": ErrorBody}})


def run_link(project: str, run_id: int) -> str:
    """The web page of a run, as a notification links to it."""
    return f"/p/{project}/runs/{run_id}"


def decision_link(decision_id: int) -> str:
    """Where the web shows a decision and its answer form."""
    return f"/inbox?decision={decision_id}"


def one_line(text: str, limit: int = TITLE_CHARS) -> str:
    """``text`` as one line of at most ``limit`` characters: control characters and runs of blanks become one space,
    and a longer line ends with "..."."""
    line = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
    if len(line) <= limit:
        return line
    return line[: limit - 3].rstrip() + "..."


def _body(text: str | None) -> str | None:
    if text is None or not text.strip():
        return None
    return runs.clip(text, runs.MAX_NOTICE_BODY_BYTES)


# The outbox

INSERT_NOTIFICATION = """
INSERT INTO notifications (user_id, kind, notice_kind, project_id, run_id, decision_id, title, body, details, link)
VALUES (%(user)s, %(kind)s, %(notice_kind)s, %(project)s, %(run)s, %(decision)s, %(title)s, %(body)s, %(details)s,
        %(link)s)
RETURNING id
"""
INSERT_DELIVERIES = """
INSERT INTO notification_deliveries (notification_id, channel_id)
SELECT %(id)s, NULL
 UNION ALL
SELECT %(id)s, c.id FROM notification_channels c WHERE c.user_id = %(user)s AND c.enabled
"""


async def notify(
    conn,
    *,
    user_id: int,
    kind: str,
    title: str,
    body: str | None = None,
    notice_kind: str | None = None,
    project_id: int | None = None,
    run_id: int | None = None,
    decision_id: int | None = None,
    details: dict | None = None,
    link: str | None = None,
) -> int:
    """Store a notification for member ``user_id`` and a delivery of it for each of their channels that is on, the
    web included, in the caller's transaction; its id. ``title`` is made one line of at most TITLE_CHARS, ``body``
    cut to MAX_NOTICE_BODY_BYTES."""
    if kind not in runs.NOTIFICATION_KINDS:
        raise ValueError(f"{kind!r} is not a kind of notification")
    params = {
        "user": user_id,
        "kind": kind,
        "notice_kind": notice_kind,
        "project": project_id,
        "run": run_id,
        "decision": decision_id,
        "title": one_line(title) or kind,
        "body": _body(body),
        "details": Jsonb(details) if details is not None else None,
        "link": link,
    }
    notification_id = (await (await conn.execute(INSERT_NOTIFICATION, params)).fetchone())[0]
    await conn.execute(INSERT_DELIVERIES, {"id": notification_id, "user": user_id})
    return notification_id


# Channels


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


class Channel:
    """Sends notifications to the channels of one kind. A job's pass makes one instance of each class it needs, with
    the hub's config (None in tests); ``send`` delivers one notification to one channel, whose ``config`` is the
    channel's own (empty for the web), and raises when it could not, so the delivery is tried again later."""

    def __init__(self, config=None):
        self.config = config

    async def send(self, notification: Outgoing, config: dict) -> None:
        raise NotImplementedError


class WebChannel(Channel):
    """The web reads notifications from the table: there is nothing to send."""

    async def send(self, notification: Outgoing, config: dict) -> None:
        return None


CHANNELS: dict[str, type[Channel]] = {WEB: WebChannel}


def backoff(attempts: int) -> timedelta:
    """How long a delivery waits after its ``attempts``-th failed try."""
    return timedelta(seconds=min(BACKOFF_SECONDS * 2 ** max(0, attempts - 1), BACKOFF_MAX_SECONDS))


DUE = """
SELECT id FROM notification_deliveries WHERE state = 'pending' AND next_at <= now() ORDER BY next_at, id LIMIT %s
"""
LOCK_DUE = """
SELECT d.attempts, c.kind, c.config, c.enabled, n.id, n.user_id, u.login, n.kind, n.notice_kind, p.name, n.run_id,
       n.decision_id, n.title, n.body, n.details, n.link, n.created_at, d.channel_id IS NOT NULL
  FROM notification_deliveries d JOIN notifications n ON n.id = d.notification_id JOIN users u ON u.id = n.user_id
  LEFT JOIN notification_channels c ON c.id = d.channel_id
  LEFT JOIN projects p ON p.id = n.project_id
 WHERE d.id = %s AND d.state = 'pending' AND d.next_at <= now()
   FOR UPDATE OF d SKIP LOCKED
"""
DELIVERED = """
UPDATE notification_deliveries SET state = 'delivered', delivered_at = now(), attempts = attempts + 1 WHERE id = %s
"""
RETRY = """
UPDATE notification_deliveries SET attempts = attempts + 1, next_at = now() + %s, last_error = %s WHERE id = %s
"""
FAILED = """
UPDATE notification_deliveries SET attempts = attempts + 1, state = 'failed', last_error = %s WHERE id = %s
"""


def _error_text(exc: BaseException) -> str:
    text = " ".join(f"{type(exc).__name__}: {exc}".split())
    return text[:MAX_ERROR_CHARS]


async def _deliver_one(conn, delivery_id: int, instances: dict, config) -> str | None:
    """Try delivery ``delivery_id`` once, in the caller's transaction; delivered, retried or failed, or None when it
    is no longer due (taken by another pass, or done meanwhile)."""
    row = await (await conn.execute(LOCK_DUE, (delivery_id,))).fetchone()
    if row is None:
        return None
    attempts, channel_kind, channel_config, enabled, *fields, has_channel = row
    notification = Outgoing(*fields)
    kind = channel_kind if has_channel else WEB
    cls = CHANNELS.get(kind)
    if cls is None:
        await conn.execute(FAILED, (f"the hub has no class for channels of kind {kind}", delivery_id))
        return "failed"
    if has_channel and not enabled:
        await conn.execute(FAILED, ("the channel was turned off before the notification went out", delivery_id))
        return "failed"
    channel = instances.get(kind)
    if channel is None:
        channel = instances[kind] = cls(config)
    try:
        await asyncio.wait_for(channel.send(notification, dict(channel_config or {})), SEND_TIMEOUT_SECONDS)
    except Exception as exc:  # any failure of a channel is the delivery's, never the job's
        error = _error_text(exc) or "the channel failed"
        if attempts + 1 >= runs.MAX_DELIVERY_ATTEMPTS:
            await conn.execute(FAILED, (error, delivery_id))
            log.warning("notification delivery failed", extra={"delivery_id": delivery_id, "channel": kind})
            return "failed"
        await conn.execute(RETRY, (backoff(attempts + 1), error, delivery_id))
        log.info("notification delivery to try again", extra={"delivery_id": delivery_id, "channel": kind})
        return "retried"
    await conn.execute(DELIVERED, (delivery_id,))
    return "delivered"


async def deliver_notifications(pool, *, config=None, batch: int = DELIVERY_BATCH) -> dict:
    """One pass of the job hub.deliver_notifications: each pending delivery that is due, tried once in a transaction
    of its own. Returns how many were delivered, are to be tried again, and failed."""
    report = {"delivered": 0, "retried": 0, "failed": 0}
    async with pool.connection() as conn:
        due = [row[0] for row in await (await conn.execute(DUE, (batch,))).fetchall()]
    instances: dict[str, Channel] = {}
    for delivery_id in due:
        async with pool.connection() as conn:
            outcome = await _deliver_one(conn, delivery_id, instances, config)
        if outcome is not None:
            report[outcome] += 1
    if report["retried"] or report["failed"]:
        log.warning("notifications delivered", extra=report)
    elif report["delivered"]:
        log.info("notifications delivered", extra=report)
    return report


# Notices from the worker


class NoticeIn(BaseModel):
    kind: Literal[runs.NOTICE_KINDS]
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
    kind: Literal[runs.NOTIFICATION_KINDS]
    notice_kind: Literal[runs.NOTICE_KINDS] | None = Field(description="a notice's kind; null for a decision")
    project: str | None
    run_id: int | None
    decision_id: int | None
    decision_state: Literal[runs.DECISION_STATES] | None = Field(
        description="the state of the decision now, for a notification of kind decision"
    )
    title: str
    body: str | None
    details: dict | None = Field(description="a notice's facts, such as the repo, branch and commits of a push")
    link: str | None = Field(description="the hub web's page that shows it, a path of the hub's own site")
    created_at: datetime
    read_at: datetime | None


NOTIFICATION_COLUMNS = """
SELECT n.id, n.kind, n.notice_kind, p.name, n.run_id, n.decision_id, d.state, n.title, n.body, n.details, n.link,
       n.created_at, n.read_at
  FROM notifications n LEFT JOIN projects p ON p.id = n.project_id LEFT JOIN decisions d ON d.id = n.decision_id
"""


def _notifications(rows) -> list[Notification]:
    return [Notification(**dict(zip(Notification.model_fields, row, strict=True))) for row in rows]


@worker_router.post(
    "/runs/{run_id}/notices",
    status_code=201,
    response_model=Notification,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def send_notice(request: Request, run_id: RunId, body: NoticeIn, user: CurrentUser) -> Notification:
    """Notify the owner of a plan run this worker holds: a push or merge into a default branch, say."""
    async with request.app.state.pool.connection() as conn:
        _, row = await _held_plan_run(conn, user, run_id, lock=True)
        _, _, _, project_id, project, plan_id, dispatcher_id, _, repos = row
        names = [entry.get("repo") for entry in repos or [] if isinstance(entry, dict)]
        if body.repo is not None and body.repo not in names:
            raise HTTPException(422, f"run {run_id} works in {', '.join(names) or 'no repo'}, not in {body.repo}")
        notification_id = await notify(
            conn,
            user_id=dispatcher_id,
            kind="notice",
            notice_kind=body.kind,
            project_id=project_id,
            run_id=run_id,
            title=body.title,
            body=body.body,
            details=body.details(),
            link=run_link(project, run_id),
        )
        shown = f"notice {body.kind}: {body.title}"
        facts = {"kind": body.kind, "notification_id": notification_id, **(body.details() or {})}
        await write_event(conn, run_id, {"text": shown, "notice": facts})
        (view,) = _notifications(
            await (await conn.execute(NOTIFICATION_COLUMNS + " WHERE n.id = %s", (notification_id,))).fetchall()
        )
    log.info("notice sent", extra={"run_id": run_id, "kind": body.kind, "plan_id": plan_id})
    return view


# The member's notifications

VISIBLE = """
 WHERE n.user_id = %(user)s
   AND (n.project_id IS NULL
        OR EXISTS (SELECT 1 FROM grants g WHERE g.user_id = n.user_id AND g.project_id = n.project_id))
"""
LIST_FILTERS = (
    VISIBLE
    + """   AND (NOT %(unread)s OR n.read_at IS NULL)
   AND (%(kind)s::text IS NULL OR n.kind = %(kind)s)
   AND (%(project)s::text IS NULL OR p.name = %(project)s)
"""
)
LIST_PAGE = (
    NOTIFICATION_COLUMNS
    + LIST_FILTERS
    + """ ORDER BY coalesce(n.kind = 'decision' AND d.state = 'open', false) DESC, n.id DESC
 LIMIT %(limit)s OFFSET %(offset)s
"""
)
LIST_TOTAL = (
    "SELECT count(*) FROM notifications n LEFT JOIN projects p ON p.id = n.project_id "
    "LEFT JOIN decisions d ON d.id = n.decision_id" + LIST_FILTERS
)
COUNTS = (
    """
SELECT count(*) FILTER (WHERE n.read_at IS NULL),
       count(*) FILTER (WHERE n.kind = 'decision' AND d.state = 'open')
  FROM notifications n LEFT JOIN decisions d ON d.id = n.decision_id
"""
    + VISIBLE
)
MARK_READ = (
    """
UPDATE notifications n SET read_at = now()
"""
    + VISIBLE
    + """   AND n.read_at IS NULL AND (%(all)s OR n.id = ANY(%(ids)s::bigint[]))
RETURNING n.id
"""
)


class NotificationList(BaseModel):
    notifications: list[Notification] = Field(description="open decisions first, then newest first")
    total: int = Field(description="notifications that match the filters")
    limit: int
    offset: int


class NotificationCount(BaseModel):
    unread: int = Field(description="notifications not read yet: the bell's number")
    open_decisions: int = Field(description="decisions that still wait for the member's answer, read or not")


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


@router.get("/notifications", response_model=NotificationList)
async def list_notifications(
    request: Request,
    user: CurrentUser,
    unread: Annotated[bool, Query(description="only the notifications not read yet")] = False,
    kind: Annotated[Literal[runs.NOTIFICATION_KINDS] | None, Query()] = None,
    project: Annotated[str | None, Query(pattern=PROJECT_NAME, description="only this project's")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> NotificationList:
    """The caller's notifications, open decisions first, then newest first."""
    params = {
        "user": user.user_id,
        "unread": unread,
        "kind": kind,
        "project": project,
        "limit": limit,
        "offset": offset,
    }
    async with request.app.state.pool.connection() as conn:
        page = _notifications(await (await conn.execute(LIST_PAGE, params)).fetchall())
        total = (await (await conn.execute(LIST_TOTAL, params)).fetchone())[0]
    return NotificationList(notifications=page, total=total, limit=limit, offset=offset)


@router.get("/notifications/count", response_model=NotificationCount)
async def count_notifications(request: Request, user: CurrentUser) -> NotificationCount:
    """How many of the caller's notifications are unread, and how many decisions wait for their answer."""
    async with request.app.state.pool.connection() as conn:
        unread, open_decisions = await (await conn.execute(COUNTS, {"user": user.user_id})).fetchone()
    return NotificationCount(unread=unread, open_decisions=open_decisions)


@router.post("/notifications/read", response_model=ReadResult, responses={422: {"model": ErrorBody}})
async def read_notifications(request: Request, body: ReadRequest, user: CurrentUser) -> ReadResult:
    """Mark the caller's notifications read: those named, or all of them."""
    params = {"user": user.user_id, "all": body.all, "ids": list(dict.fromkeys(body.ids or []))}
    async with request.app.state.pool.connection() as conn:
        marked = sorted(row[0] for row in await (await conn.execute(MARK_READ, params)).fetchall())
        if marked:
            target = "notifications:all" if body.all else "notifications:" + ",".join(map(str, marked))
            if body.all:
                target += f" count={len(marked)}"
            await audit.record(
                conn, actor_id=user.user_id, token_id=user.token_id, action=audit.NOTIFICATION_READ, target=target
            )
        unread = (await (await conn.execute(COUNTS, {"user": user.user_id})).fetchone())[0]
    log.info("notifications read", extra={"login": user.login, "read": len(marked)})
    return ReadResult(read=len(marked), unread=unread)
