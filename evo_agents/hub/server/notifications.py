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
try to the next up to BACKOFF_MAX_SECONDS, and its MAX_DELIVERY_ATTEMPTS-th failure marks it failed. Three exceptions
say more: RetryAfter (the service asked to wait, such as Telegram's 429 with retry_after) moves ``next_at`` that far
without counting a failed try; Undeliverable fails the delivery at once; ChannelGone fails it and turns the channel off
with its reason in the channel's config (``disabled_reason``), as when a member blocked the bot. A kind with no class
in CHANNELS, or a channel turned off since, fails its delivery at once with that reason, so a hub that drops a channel
does not try it forever. Adding a channel takes a subclass of Channel and its entry in CHANNELS; the tables, the outbox
and the job stay as they are. A class may read more of a notification first (``prepare``, in the delivery's
transaction), and ``send`` may answer the id the service gave the message, kept as the delivery's ``external_id``.

Before a channel of a row gets a notification, the job checks two things as they stand at the delivery, not as they
stood when it was stored. The member must still hold a grant on the notification's project (NO_GRANT fails the
delivery at once; the channel stays). And a channel bound to the web session that linked it (``token_id``), or of a
class that needs one (``Channel.needs_session``, as Telegram's does), lives no longer than that session: once it is
revoked, signed out or expired, the delivery fails and the channel is turned off with SESSION_ENDED as its reason.

POST /v1/worker/runs/{id}/notices takes a notice (``runs.NOTICE_KINDS``) from the worker holding a plan run: a title,
a body, and the repo, branch and commits of a push or merge. It notifies the run's owner, links to the run's page and
leaves a ``system`` event in the run's log. A run of one step sends no notice (404, as any run the worker does not
hold). The hub sends ``plan_finished`` and ``run_failed`` itself when a plan run ends (``run_state``).

A member reads their own notifications: GET /v1/me/notifications (open decisions and proposals first, then newest
first; filtered by unread, kind and project, a page at a time), GET /v1/me/notifications/count (the unread ones, and
the decisions and the proposals still waiting for an answer, for the bell), and POST /v1/me/notifications/read (by ids,
or all), audited as notification.read with the ids, never the text. A notification of a project the member no longer
holds a grant on is not shown. Answering a decision reads its notification too (``decisions``). A notification of kind
proposal is a tier 2 proposal of the Curator in the member's Inbox (``proposals``), and answering it reads it too.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import (
    BigInteger,
    and_,
    bindparam,
    exists,
    false,
    func,
    insert,
    literal,
    null,
    or_,
    select,
    union_all,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import review, runs, tables
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models import LINE, MAX_ID, OBJECT_NAME, REFUSALS, RunId
from evo_agents.hub.server.runs.service.plan_runs import held_plan_run
from evo_agents.hub.server.security import WEB as WEB_SESSION
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

WEB = "web"  # the channel every member has, without a row: a delivery whose channel_id is NULL
TITLE_CHARS = 200  # notifications.title, as schema 0010 bounds it
MAX_ERROR_CHARS = 2000  # notification_deliveries.last_error
MAX_EXTERNAL_ID_CHARS = 200  # notification_deliveries.external_id, as schema 0015 bounds it
DELIVERY_BATCH = 500  # deliveries one pass of the job takes
SEND_TIMEOUT_SECONDS = 30.0  # a channel's send that takes longer counts as failed
BACKOFF_SECONDS = 60  # after the first failed try; doubled after each next one
BACKOFF_MAX_SECONDS = 3600
MAX_LIST = 200
MAX_OFFSET = 100_000
MAX_READ_IDS = 500
NO_GRANT = "the member holds no grant on the notification's project any more"
SESSION_ENDED = "the web session that linked this channel ended (signed out, revoked or expired): link it again"

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


def _deliveries(notification_id: int, user_id: int):
    """A delivery of notification ``notification_id`` on the web (no channel), then one for each channel of member
    ``user_id`` that is enabled."""
    channels = tables.notification_channels
    notification = literal(notification_id, BigInteger)
    rows = union_all(
        select(notification, null()),
        select(notification, channels.c.id).where(channels.c.user_id == user_id, channels.c.enabled),
    )
    deliveries = tables.notification_deliveries
    return insert(deliveries).from_select([deliveries.c.notification_id, deliveries.c.channel_id], rows)


async def notify(
    conn: AsyncConnection,
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
    proposal_id: int | None = None,
) -> int:
    """Store a notification for member ``user_id`` and a delivery of it for each of their channels that is on, the
    web included, in the caller's transaction; its id. ``title`` is made one line of at most TITLE_CHARS, ``body``
    cut to MAX_NOTICE_BODY_BYTES."""
    if kind not in runs.NOTIFICATION_KINDS:
        raise ValueError(f"{kind!r} is not a kind of notification")
    stored = tables.notifications
    inserted = await conn.execute(
        insert(stored)
        .values(
            user_id=user_id,
            kind=kind,
            notice_kind=notice_kind,
            project_id=project_id,
            run_id=run_id,
            decision_id=decision_id,
            proposal_id=proposal_id,
            title=one_line(title) or kind,
            body=_body(body),
            details=details if details is not None else null(),  # SQL NULL: None alone would store JSON null
            link=link,
        )
        .returning(stored.c.id)
    )
    notification_id = inserted.scalar_one()
    await conn.execute(_deliveries(notification_id, user_id))
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
    proposal_id: int | None = None
    extra: dict | None = None  # what a channel's ``prepare`` read besides the notification


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


class Channel:
    """Sends notifications to the channels of one kind. A job's pass makes one instance of each class it needs, with
    the hub's config (None in tests); ``prepare`` may read more of a notification in the delivery's transaction, and
    ``send`` delivers it to one channel, whose ``config`` is the channel's own (empty for the web), answers the id the
    service gave the message (or None), and raises when it could not, so the delivery is tried again later."""

    needs_session = False  # a channel of this class lives no longer than the web session that linked it

    def __init__(self, config=None):
        self.config = config

    async def prepare(self, conn: AsyncConnection, notification: Outgoing) -> Outgoing:
        return notification

    async def send(self, notification: Outgoing, config: dict) -> str | None:
        raise NotImplementedError


class WebChannel(Channel):
    """The web reads notifications from the table: there is nothing to send."""

    async def send(self, notification: Outgoing, config: dict) -> str | None:
        return None


CHANNELS: dict[str, type[Channel]] = {WEB: WebChannel}


def channel_class(kind: str) -> type[Channel] | None:
    """The class CHANNELS names for ``kind``, once every channel the hub ships has registered its class (importing a
    channel's module adds it, as ``evo_agents.hub.server.telegram`` does for kind telegram)."""
    from evo_agents.hub.server import telegram  # noqa: F401 (it registers TelegramChannel)

    return CHANNELS.get(kind)


def backoff(attempts: int) -> timedelta:
    """How long a delivery waits after its ``attempts``-th failed try."""
    return timedelta(seconds=min(BACKOFF_SECONDS * 2 ** max(0, attempts - 1), BACKOFF_MAX_SECONDS))


def _due(batch: int):
    """The first ``batch`` pending deliveries whose ``next_at`` has passed, the longest due first."""
    deliveries = tables.notification_deliveries
    return (
        select(deliveries.c.id)
        .where(deliveries.c.state == "pending", deliveries.c.next_at <= func.now())
        .order_by(deliveries.c.next_at, deliveries.c.id)
        .limit(batch)
    )


OUTGOING = tuple(field.name for field in dataclasses.fields(Outgoing) if field.name != "extra")


def _lock_due(delivery_id: int):
    """Delivery ``delivery_id`` while it is pending and due, locked, with its channel and its notification as
    Outgoing names the columns, whether the member holds a grant on its project now (``granted``) and whether the
    web session its channel is bound to lives (``session_live``); no row when another pass holds it."""
    deliveries, channels = tables.notification_deliveries, tables.notification_channels
    stored, users, projects = tables.notifications, tables.users, tables.projects
    grants, tokens = tables.grants, tables.tokens
    granted = or_(
        stored.c.project_id.is_(None),
        exists().where(grants.c.user_id == stored.c.user_id, grants.c.project_id == stored.c.project_id),
    )
    session_live = exists().where(
        tokens.c.id == channels.c.token_id,
        tokens.c.user_id == stored.c.user_id,
        tokens.c.kind == WEB_SESSION,
        tokens.c.revoked_at.is_(None),
        tokens.c.expires_at > func.now(),
    )
    return (
        select(
            deliveries.c.attempts,
            channels.c.kind.label("channel_kind"),
            channels.c.config.label("channel_config"),
            channels.c.enabled,
            stored.c.id,
            stored.c.user_id,
            users.c.login,
            stored.c.kind,
            stored.c.notice_kind,
            projects.c.name.label("project"),
            stored.c.run_id,
            stored.c.decision_id,
            stored.c.title,
            stored.c.body,
            stored.c.details,
            stored.c.link,
            stored.c.created_at,
            stored.c.proposal_id,
            deliveries.c.channel_id,
            deliveries.c.channel_id.is_not(None).label("has_channel"),
            channels.c.token_id,
            granted.label("granted"),
            session_live.label("session_live"),
        )
        .select_from(
            deliveries.join(stored, stored.c.id == deliveries.c.notification_id)
            .join(users, users.c.id == stored.c.user_id)
            .outerjoin(channels, channels.c.id == deliveries.c.channel_id)
            .outerjoin(projects, projects.c.id == stored.c.project_id)
        )
        .where(deliveries.c.id == delivery_id, deliveries.c.state == "pending", deliveries.c.next_at <= func.now())
        .with_for_update(of=deliveries, skip_locked=True)
    )


def _delivered(delivery_id: int, external_id: str | None = None):
    deliveries = tables.notification_deliveries
    return (
        update(deliveries)
        .values(
            state="delivered",
            delivered_at=func.now(),
            attempts=deliveries.c.attempts + 1,
            external_id=external_id[:MAX_EXTERNAL_ID_CHARS] if external_id else None,
        )
        .where(deliveries.c.id == delivery_id)
    )


def _later(delivery_id: int, wait: timedelta, error: str):
    """Try delivery ``delivery_id`` again ``wait`` from now, as its service asked, without counting a failed try."""
    deliveries = tables.notification_deliveries
    return update(deliveries).values(next_at=func.now() + wait, last_error=error).where(deliveries.c.id == delivery_id)


def _turn_off(channel_id: int, reason: str):
    """Channel ``channel_id`` off, with ``reason`` and the time in its config, as the member's settings show it."""
    channels = tables.notification_channels
    said = {"disabled_reason": reason[:MAX_ERROR_CHARS]}
    return (
        update(channels)
        .values(
            enabled=False,
            config=channels.c.config.op("||")(bindparam("said", said, type_=JSONB)).op("||")(
                func.jsonb_build_object("disabled_at", func.now())
            ),
        )
        .where(channels.c.id == channel_id)
    )


def _retry(delivery_id: int, wait: timedelta, error: str):
    deliveries = tables.notification_deliveries
    return (
        update(deliveries)
        .values(attempts=deliveries.c.attempts + 1, next_at=func.now() + wait, last_error=error)
        .where(deliveries.c.id == delivery_id)
    )


def _failed(delivery_id: int, error: str):
    deliveries = tables.notification_deliveries
    return (
        update(deliveries)
        .values(attempts=deliveries.c.attempts + 1, state="failed", last_error=error)
        .where(deliveries.c.id == delivery_id)
    )


def _error_text(exc: BaseException) -> str:
    text = " ".join(f"{type(exc).__name__}: {exc}".split())
    return text[:MAX_ERROR_CHARS]


async def _deliver_one(conn: AsyncConnection, delivery_id: int, instances: dict, config) -> str | None:
    """Try delivery ``delivery_id`` once, in the caller's transaction; delivered, retried or failed, or None when it
    is no longer due (taken by another pass, or done meanwhile)."""
    row = (await conn.execute(_lock_due(delivery_id))).one_or_none()
    if row is None:
        return None
    found = row._mapping
    attempts, has_channel = row.attempts, row.has_channel
    notification = Outgoing(**{name: found[name] for name in OUTGOING})
    kind = row.channel_kind if has_channel else WEB
    cls = channel_class(kind)
    if cls is None:
        await conn.execute(_failed(delivery_id, f"the hub has no class for channels of kind {kind}"))
        return "failed"
    if has_channel and not row.enabled:
        await conn.execute(_failed(delivery_id, "the channel was turned off before the notification went out"))
        return "failed"
    if has_channel and not row.granted:
        await conn.execute(_failed(delivery_id, NO_GRANT))
        log.info("notification not delivered: no grant", extra={"delivery_id": delivery_id, "channel": kind})
        return "failed"
    if has_channel and (cls.needs_session or row.token_id is not None) and not row.session_live:
        await conn.execute(_failed(delivery_id, SESSION_ENDED))
        await conn.execute(_turn_off(row.channel_id, SESSION_ENDED))
        log.info("notification channel off: its session ended", extra={"delivery_id": delivery_id, "channel": kind})
        return "failed"
    channel = instances.get(kind)
    if channel is None:
        channel = instances[kind] = cls(config)
    try:
        notification = await channel.prepare(conn, notification)
        sent = await asyncio.wait_for(channel.send(notification, dict(row.channel_config or {})), SEND_TIMEOUT_SECONDS)
    except RetryAfter as exc:
        await conn.execute(_later(delivery_id, timedelta(seconds=exc.seconds), _error_text(exc)))
        log.info(
            "notification delivery waits as its channel asked", extra={"delivery_id": delivery_id, "channel": kind}
        )
        return "retried"
    except Undeliverable as exc:
        error = _error_text(exc)
        await conn.execute(_failed(delivery_id, error))
        if isinstance(exc, ChannelGone) and row.channel_id is not None:
            await conn.execute(_turn_off(row.channel_id, str(exc) or error))
        log.warning("notification undeliverable", extra={"delivery_id": delivery_id, "channel": kind})
        return "failed"
    except Exception as exc:  # any failure of a channel is the delivery's, never the job's
        error = _error_text(exc) or "the channel failed"
        if attempts + 1 >= runs.MAX_DELIVERY_ATTEMPTS:
            await conn.execute(_failed(delivery_id, error))
            log.warning("notification delivery failed", extra={"delivery_id": delivery_id, "channel": kind})
            return "failed"
        await conn.execute(_retry(delivery_id, backoff(attempts + 1), error))
        log.info("notification delivery to try again", extra={"delivery_id": delivery_id, "channel": kind})
        return "retried"
    await conn.execute(_delivered(delivery_id, sent))
    return "delivered"


async def deliver_notifications(engine, *, config=None, batch: int = DELIVERY_BATCH) -> dict:
    """One pass of the job hub.deliver_notifications: each pending delivery that is due, tried once in a transaction
    of its own. Returns how many were delivered, are to be tried again, and failed."""
    report = {"delivered": 0, "retried": 0, "failed": 0}
    async with engine.begin() as conn:
        due = (await conn.execute(_due(batch))).scalars().all()
    instances: dict[str, Channel] = {}
    for delivery_id in due:
        async with engine.begin() as conn:
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
    kind: Literal[runs.WORKER_NOTICE_KINDS]
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
    proposal_id: int | None = Field(None, description="the proposal, for a notification of kind proposal")
    proposal_state: Literal[review.PROPOSAL_STATES] | None = Field(
        None, description="the state of the proposal now, for a notification of kind proposal"
    )
    title: str
    body: str | None
    details: dict | None = Field(description="a notice's facts, such as the repo, branch and commits of a push")
    link: str | None = Field(description="the hub web's page that shows it, a path of the hub's own site")
    created_at: datetime
    read_at: datetime | None


def _shown():
    """Notifications with their project's name, their decision's state and their proposal's: the FROM of the member's
    routes."""
    stored, projects, decisions, proposals = tables.notifications, tables.projects, tables.decisions, tables.proposals
    return (
        stored.outerjoin(projects, projects.c.id == stored.c.project_id)
        .outerjoin(decisions, decisions.c.id == stored.c.decision_id)
        .outerjoin(proposals, proposals.c.id == stored.c.proposal_id)
    )


def _notification_rows():
    """The columns of Notification, by its field names, over ``_shown``."""
    stored = tables.notifications
    return select(
        stored.c.id,
        stored.c.kind,
        stored.c.notice_kind,
        tables.projects.c.name.label("project"),
        stored.c.run_id,
        stored.c.decision_id,
        tables.decisions.c.state.label("decision_state"),
        stored.c.proposal_id,
        tables.proposals.c.state.label("proposal_state"),
        stored.c.title,
        stored.c.body,
        stored.c.details,
        stored.c.link,
        stored.c.created_at,
        stored.c.read_at,
    ).select_from(_shown())


def _notifications(rows) -> list[Notification]:
    return [Notification(**row._mapping) for row in rows]


@worker_router.post(
    "/runs/{run_id}/notices",
    status_code=201,
    response_model=Notification,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def send_notice(request: Request, run_id: RunId, body: NoticeIn, user: CurrentUser) -> Notification:
    """Notify the owner of a plan run this worker holds: a push or merge into a default branch, say."""
    async with request.app.state.engine.begin() as conn:
        _, row = await held_plan_run(conn, user, run_id, lock=True)
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
        stored = _notification_rows().where(tables.notifications.c.id == notification_id)
        (view,) = _notifications((await conn.execute(stored)).all())
    log.info("notice sent", extra={"run_id": run_id, "kind": body.kind, "plan_id": plan_id})
    return view


# The member's notifications


def _visible(user_id: int):
    """The notifications of member ``user_id`` that are of no project, or of a project they hold a grant on."""
    stored, grants = tables.notifications, tables.grants
    granted = exists().where(grants.c.user_id == stored.c.user_id, grants.c.project_id == stored.c.project_id)
    return and_(stored.c.user_id == user_id, or_(stored.c.project_id.is_(None), granted))


def _open_decision():
    """A notification of a decision that still waits for its answer."""
    return and_(tables.notifications.c.kind == "decision", tables.decisions.c.state == "open")


def _open_proposal():
    """A notification of a proposal that still waits for its answer."""
    return and_(tables.notifications.c.kind == "proposal", tables.proposals.c.state == "open")


def _listed(user_id: int, unread: bool, kind: str | None, project: str | None) -> list:
    """The conditions of the list's filters, over ``_shown``."""
    stored = tables.notifications
    found = [_visible(user_id)]
    if unread:
        found.append(stored.c.read_at.is_(None))
    if kind is not None:
        found.append(stored.c.kind == kind)
    if project is not None:
        found.append(tables.projects.c.name == project)
    return found


def _counts(user_id: int):
    """How many of the member's notifications are unread, and how many are of a decision or a proposal still open."""
    stored = tables.notifications
    return (
        select(
            func.count().filter(stored.c.read_at.is_(None)).label("unread"),
            func.count().filter(_open_decision()).label("open_decisions"),
            func.count().filter(_open_proposal()).label("open_proposals"),
        )
        .select_from(_shown())
        .where(_visible(user_id))
    )


def _mark_read(user_id: int, ids: list[int] | None):
    """Mark the member's unread notifications read, those of ``ids`` or all of them when it is None; their ids."""
    stored = tables.notifications
    marked = update(stored).values(read_at=func.now()).where(_visible(user_id), stored.c.read_at.is_(None))
    if ids is not None:
        marked = marked.where(stored.c.id.in_(ids))
    return marked.returning(stored.c.id)


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
    """The caller's notifications, open decisions and proposals first, then newest first."""
    found = _listed(user.user_id, unread, kind, project)
    waiting = or_(_open_decision(), _open_proposal())
    order = (func.coalesce(waiting, false()).desc(), tables.notifications.c.id.desc())
    listed = _notification_rows().where(*found).order_by(*order).limit(limit).offset(offset)
    total = select(func.count()).select_from(_shown()).where(*found)
    async with request.app.state.engine.begin() as conn:
        page = _notifications((await conn.execute(listed)).all())
        matching = (await conn.execute(total)).scalar_one()
    return NotificationList(notifications=page, total=matching, limit=limit, offset=offset)


@router.get("/notifications/count", response_model=NotificationCount)
async def count_notifications(request: Request, user: CurrentUser) -> NotificationCount:
    """How many of the caller's notifications are unread, and how many decisions wait for their answer."""
    async with request.app.state.engine.begin() as conn:
        counted = (await conn.execute(_counts(user.user_id))).one()
    return NotificationCount(**counted._mapping)


@router.post("/notifications/read", response_model=ReadResult, responses={422: {"model": ErrorBody}})
async def read_notifications(request: Request, body: ReadRequest, user: CurrentUser) -> ReadResult:
    """Mark the caller's notifications read: those named, or all of them."""
    ids = None if body.all else list(dict.fromkeys(body.ids or []))
    async with request.app.state.engine.begin() as conn:
        marked = sorted((await conn.execute(_mark_read(user.user_id, ids))).scalars().all())
        if marked:
            target = "notifications:all" if body.all else "notifications:" + ",".join(map(str, marked))
            if body.all:
                target += f" count={len(marked)}"
            await audit.record(
                conn, actor_id=user.user_id, token_id=user.token_id, action=audit.NOTIFICATION_READ, target=target
            )
        unread = (await conn.execute(_counts(user.user_id))).one().unread
    log.info("notifications read", extra={"login": user.login, "read": len(marked)})
    return ReadResult(read=len(marked), unread=unread)
