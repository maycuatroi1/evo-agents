"""Notifications: the outbox every decision and notice of a plan run goes through, the channels that deliver them, and
the notices of the worker; the routes a member reads them with are ``notification_list``. ``docs/notifications.md`` is the design and ``runs`` holds the kinds and limits.

``notify`` stores, in the caller's transaction, one notification for one member and one delivery of it for each
channel of the member's that is on: the web, which every member has without a row of notification_channels (a
delivery without a channel), and each enabled row. The table is the outbox: nothing is sent while the transaction is
open, and a hub that stops between the commit and the send loses nothing. A notification of a project also goes to
the channels of the isolated plugins the project enabled and granted notifications:send, through the plugins' own
outbox (``evo_agents.hub.plugin_events``). A notification of the interrupt tier for its member
(``evo_agents.hub.server.push_tiers``) also gets one delivery on each push device of its member that reaches them (a
delivery with a ``device_id``, ``evo_agents.hub.server.push_store``).

``deliver_notifications`` is the job hub.deliver_notifications, every minute, and queued by each transaction that
stores a notification (``bus.soon``), so it goes out once that commits; with the event bus on, the delivery process
(``evo-agents hub deliver``) runs that pass instead, as soon as the broker carries the news of the commit. It takes the pending deliveries whose
``next_at`` has passed, each in a transaction of its own with ``FOR UPDATE SKIP LOCKED``, and hands each to an
instance of the class CHANNELS names for its channel's kind (WEB for a delivery without a channel). The web's class
sends nothing, since the web reads notifications from the table, so its deliveries are delivered at once. A class
that raises leaves the delivery pending with ``last_error`` and a ``next_at`` BACKOFF_SECONDS later, doubling from one
try to the next up to BACKOFF_MAX_SECONDS, and its MAX_DELIVERY_ATTEMPTS-th failure marks it failed. Three exceptions
say more: RetryAfter (the service asked to wait, such as Telegram's 429 with retry_after) moves ``next_at`` that far
without counting a failed try; Undeliverable fails the delivery at once; ChannelGone fails it and turns the channel off
with its reason in the channel's config (``disabled_reason``), as when a member blocked the bot. A kind with no class
in CHANNELS, or a channel turned off since, fails its delivery at once with that reason, so a hub that drops a channel
does not try it forever. So does a kind whose class a core plugin contributes but does not load, or does not make (its
import or its ``__init__`` raises): ``last_error`` says why, and the deliveries of every other kind, the web's
included, go on. Adding a channel takes a subclass of Channel, contributed by a core plugin to the channels
under the kind's name (as the core plugin telegram does), or an entry in CHANNELS, a class or the ``module:name`` of
one, imported when a pass first needs it (as the senders of push devices are, ``push_senders``); the tables, the
outbox and the job stay as they are. A class may read more of a notification first (``prepare``, in a savepoint of the
delivery's transaction), and ``send`` may answer the id the service gave the message, kept as the delivery's
``external_id``. A database error in one delivery is a failed try of that delivery alone: the others of the pass go on.

A delivery of a decision on a channel, and the edit of its message, is tried for DECISION_WINDOW from when it was
stored, which is its first try since the job runs once the transaction commits (question #99): its retries keep to
RetryAfter or the backoff above, each ``next_at`` no later than the end of the window, and a try that fails at or
after that end fails the delivery with its error, RetryAfter included. The decision stays open on the hub, and its
view says the channel failed (``decisions.service.channels``). While a class's ``prepare`` runs, SENDING holds the delivery it
prepares for: its id, the kind of its channel, and for an edit the delivery it edits and that message's id, which
``member_channels.prepare`` reads to judge a decision by its label against the channel's sink and to tell the class to
edit. A delivery sent without the notification's content (``extra["restricted"]``) keeps why in ``withheld``.

Once a decision is answered, cancelled or expired, each channel that sent its message (a delivery with the message's
id) gets one delivery more of the same notification, ``edit_of`` naming the first: ``queue_edits``, at the start of
each pass and queued by each transaction that answers a decision, stores them and marks the first ``settled_at``. The
edit goes out as any delivery does, under the same window, so a class that failed to edit its message after an answer
is tried again without touching the answer.

Before a channel of a row gets a notification, the job checks two things as they stand at the delivery, not as they
stood when it was stored. The member must still hold a grant on the notification's project (NO_GRANT fails the
delivery at once; the channel stays). And a channel bound to the web session that linked it (``token_id``), or of a
class that needs one (``Channel.needs_session``, as Telegram's does), lives no longer than that session: once it is
revoked, signed out or expired, the delivery fails and the channel is turned off with SESSION_ENDED as its reason. A
channel a hub admin linked (``member_channels.admin_link``) is bound to no session and counts as one whose session
lives.

A delivery to a push device goes to the class CHANNELS names for the device's kind, with the device's address and
params as the channel's config, after the same check of the grant. A device lives no longer than the token that added
it: once that token is revoked or expired, the delivery fails and the device is removed. ChannelGone from a device's
class (the push service no longer knows it) removes that device alone, with its deliveries; a delivery that went out
marks when the device last got a push. The member's quiet hours may hold a push (``push_tiers.held_until``): its
``next_at`` moves to when it may go, without counting a try, and the pass counts it nowhere. A push of a decision that
is no longer open fails with DECISION_CLOSED instead of asking what nobody asks any more.

POST /v1/worker/runs/{id}/notices takes a notice (``runs.NOTICE_KINDS``) from the worker holding a plan run: a title,
a body, and the repo, branch and commits of a push or merge. It notifies the run's owner, links to the run's page and
leaves a ``system`` event in the run's log. A run of one step sends no notice (404, as any run the worker does not
hold). The hub sends ``plan_finished`` and ``run_failed`` itself when a plan run ends (``run_state``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
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
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import bus, jobs, plugin_events, review, runs, tables
from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.plugin_calls import core_registry, load
from evo_agents.hub.server import audit, push_store, push_tiers
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.ops_views import OpsHolding
from evo_agents.hub.server.output_views import RunOutput
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models import LINE, OBJECT_NAME, REFUSALS, RunId
from evo_agents.hub.server.runs.service.plan_runs import held_plan_run
from evo_agents.hub.server.security import WEB as WEB_SESSION
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.tables_push import APNS, FCM, WEBPUSH

log = logging.getLogger(__name__)

WEB = "web"  # the channel every member has, without a row: a delivery whose channel_id is NULL
TITLE_CHARS = 200  # notifications.title, as schema 0010 bounds it
MAX_ERROR_CHARS = 2000  # notification_deliveries.last_error
MAX_EXTERNAL_ID_CHARS = 200  # notification_deliveries.external_id, as schema 0015 bounds it
DELIVERY_BATCH = 500  # deliveries one pass of the job takes
SEND_TIMEOUT_SECONDS = 30.0  # a channel's send that takes longer counts as failed
BACKOFF_SECONDS = 60  # after the first failed try; doubled after each next one
BACKOFF_MAX_SECONDS = 3600

NO_GRANT = "the member holds no grant on the notification's project any more"
# The blob store of the job's pass, so a channel's prepare can hand out presigned GETs of a notification's files
# (``output_notices.channel_outputs``); None outside a pass, or on a hub without one.
PASS_BLOBS: ContextVar[BlobStore | None] = ContextVar("notification_pass_blobs", default=None)
SESSION_ENDED = "the web session that linked this channel ended (signed out, revoked or expired): link it again"
DECISION_CLOSED = "the decision was answered or closed before its push went out"
DECISION_WINDOW = timedelta(minutes=30)  # a channel's delivery of a decision, or its edit, is tried this long
EDIT_BATCH = 500  # first deliveries one sweep of queue_edits gives an edit

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


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


def _deliveries(notification_id: int, user_id: int, *, web_only: bool = False):
    """A delivery of notification ``notification_id`` on the web (no channel), then, unless ``web_only``, one for
    each channel of member ``user_id`` that is enabled."""
    channels = tables.notification_channels
    notification = literal(notification_id, BigInteger)
    web = select(notification, null())
    rows = (
        web
        if web_only
        else union_all(
            web, select(notification, channels.c.id).where(channels.c.user_id == user_id, channels.c.enabled)
        )
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
    cut to MAX_NOTICE_BODY_BYTES. A notice that only reports (``runs.report_only``) is stored read and delivered on
    the web alone: no channel of the member's gets it, nor a push device unless the member chose the interrupt tier for
    its kind, and Today's summary counts it instead."""
    if kind not in runs.NOTIFICATION_KINDS:
        raise ValueError(f"{kind!r} is not a kind of notification")
    stored = tables.notifications
    report = runs.report_only(kind, notice_kind, details)
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
            read_at=func.now() if report else null(),
        )
        .returning(stored.c.id)
    )
    notification_id = inserted.scalar_one()
    await conn.execute(_deliveries(notification_id, user_id, web_only=report))
    # A report's kinds are of the brief tier by default (``push_tiers``): a device gets one only when its member chose
    # the interrupt tier for its kind themselves.
    tier = {"notice_kind": notice_kind, "run_id": run_id, "decision_id": decision_id, "details": details}
    if await push_tiers.interrupts(conn, user_id=user_id, kind=kind, **tier):
        await conn.execute(push_store.device_deliveries(notification_id, user_id))
    await plugin_events.emit_notification(conn, notification_id, project_id)  # the channels of isolated plugins
    await bus.soon(jobs.DELIVER_NOTIFICATIONS, conn)  # goes out once the transaction commits
    return notification_id


# Channels


@dataclass(frozen=True)
class Sending:
    """The delivery a channel's ``prepare`` prepares for (SENDING): its id, the kind of its channel, and for an edit
    the delivery whose message it edits and that message's id."""

    delivery_id: int
    channel: str
    edit_of: int | None = None
    edit_external_id: str | None = None


# The delivery of the job's pass whose class is preparing now; None outside a pass's prepare.
SENDING: ContextVar[Sending | None] = ContextVar("notification_sending", default=None)


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


# The classes of the channels the hub has itself: a class, or the ``module:name`` of one, imported when needed.
CHANNELS: dict[str, type[Channel] | str] = {
    WEB: WebChannel,
    WEBPUSH: "evo_agents.hub.server.push_senders:WebPushChannel",  # the devices of push_devices of kind webpush
    APNS: "evo_agents.hub.server.push_native:ApnsChannel",  # ... of kind apns
    FCM: "evo_agents.hub.server.push_native:FcmChannel",  # ... of kind fcm
}


def _channel_class(kind: str) -> tuple[type[Channel] | None, str]:
    """(the class of channels of ``kind``, or None) and, for None, why: no class, or one that did not load."""
    if kind in CHANNELS:
        named = CHANNELS[kind]
        if not isinstance(named, str):
            return named, ""
        try:
            return load(named), ""
        except Exception as exc:  # a class that does not import fails its own kind's deliveries, never the job
            log.warning("notification channel not loaded", extra={"channel": kind}, exc_info=True)
            return None, f"the class of channels of kind {kind} ({named}) did not load: {_error_text(exc)}"
    contributed = core_registry().contributions("channels")
    found = next(
        ((manifest, item) for manifest, item in contributed if item["name"] == kind and "python" in item), None
    )
    if found is None:
        return None, f"the hub has no class for channels of kind {kind}"
    manifest, item = found
    try:
        return load(item["python"]), ""
    except Exception as exc:  # a core plugin's broken class fails its own kind's deliveries, never the job
        log.warning("core plugin channel not loaded", extra={"contribution": f"{manifest.id}:{kind}"}, exc_info=True)
        return None, f"the class of channels of kind {kind} (plugin {manifest.id}) did not load: {_error_text(exc)}"


def channel_class(kind: str) -> type[Channel] | None:
    """The class CHANNELS names for ``kind``, else the one a core plugin contributes to the channels under that name
    (``evo_agents.hub.plugin_calls``), as the core plugin telegram does for kind telegram; None when there is none,
    or when the plugin's class does not load (logged)."""
    return _channel_class(kind)[0]


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
    """Delivery ``delivery_id`` while it is pending and due, locked, with its channel or its device and its
    notification as Outgoing names the columns, whether the member holds a grant on its project now (``granted``),
    whether the web session its channel is bound to lives (``session_live``) and whether the token that added its
    device does (``device_live``); no row when another pass holds it."""
    deliveries, channels, devices = tables.notification_deliveries, tables.notification_channels, tables.push_devices
    stored, users, projects = tables.notifications, tables.users, tables.projects
    grants, tokens, decisions = tables.grants, tables.tokens, tables.decisions
    edited = deliveries.alias("edited")
    decision_open = exists().where(decisions.c.id == stored.c.decision_id, decisions.c.state == "open")
    granted = or_(
        stored.c.project_id.is_(None),
        exists().where(grants.c.user_id == stored.c.user_id, grants.c.project_id == stored.c.project_id),
    )
    session_live = or_(
        exists().where(
            tokens.c.id == channels.c.token_id,
            tokens.c.user_id == stored.c.user_id,
            tokens.c.kind == WEB_SESSION,
            tokens.c.revoked_at.is_(None),
            tokens.c.expires_at > func.now(),
        ),
        and_(channels.c.token_id.is_(None), channels.c.linked_by.is_not(None)),  # a hub admin linked it (0268)
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
            deliveries.c.device_id,
            devices.c.kind.label("device_kind"),
            devices.c.address.label("device_address"),
            devices.c.params.label("device_params"),
            push_store.device_live(devices).label("device_live"),
            deliveries.c.edit_of,
            edited.c.external_id.label("edit_external_id"),
            decision_open.label("decision_open"),
            (deliveries.c.created_at + DECISION_WINDOW <= func.now()).label("past_window"),
        )
        .select_from(
            deliveries.join(stored, stored.c.id == deliveries.c.notification_id)
            .join(users, users.c.id == stored.c.user_id)
            .outerjoin(edited, edited.c.id == deliveries.c.edit_of)
            .outerjoin(channels, channels.c.id == deliveries.c.channel_id)
            .outerjoin(devices, devices.c.id == deliveries.c.device_id)
            .outerjoin(projects, projects.c.id == stored.c.project_id)
        )
        .where(deliveries.c.id == delivery_id, deliveries.c.state == "pending", deliveries.c.next_at <= func.now())
        .with_for_update(of=deliveries, skip_locked=True)
    )


def _delivered(delivery_id: int, external_id: str | None = None, *, withheld: str | None = None, settled: bool = True):
    """Delivery ``delivery_id`` delivered as ``external_id``, sent without the notification's content for
    ``withheld``; ``settled`` unless its message may need an edit later (a decision still open)."""
    deliveries = tables.notification_deliveries
    return (
        update(deliveries)
        .values(
            state="delivered",
            delivered_at=func.now(),
            attempts=deliveries.c.attempts + 1,
            external_id=external_id[:MAX_EXTERNAL_ID_CHARS] if external_id else None,
            withheld=withheld,
            settled_at=func.now() if settled else None,
        )
        .where(deliveries.c.id == delivery_id)
    )


def _next_at(wait: timedelta, windowed: bool):
    """``wait`` from now, no later than the end of the delivery's window when it has one (DECISION_WINDOW)."""
    deliveries = tables.notification_deliveries
    later = func.now() + wait
    return func.least(later, deliveries.c.created_at + DECISION_WINDOW) if windowed else later


def _later(delivery_id: int, wait: timedelta, error: str, windowed: bool = False):
    """Try delivery ``delivery_id`` again ``wait`` from now, as its service asked, without counting a failed try."""
    deliveries = tables.notification_deliveries
    return (
        update(deliveries)
        .values(next_at=_next_at(wait, windowed), last_error=error)
        .where(deliveries.c.id == delivery_id)
    )


def _held(delivery_id: int, until: datetime):
    """Delivery ``delivery_id`` held until ``until`` by its member's quiet hours, without counting a try."""
    deliveries = tables.notification_deliveries
    return update(deliveries).values(next_at=until).where(deliveries.c.id == delivery_id)


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


def _retry(delivery_id: int, wait: timedelta, error: str, windowed: bool = False):
    deliveries = tables.notification_deliveries
    return (
        update(deliveries)
        .values(attempts=deliveries.c.attempts + 1, next_at=_next_at(wait, windowed), last_error=error)
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


def _target(row) -> tuple[str, dict]:
    """The kind of the class a locked delivery goes to, and the config its ``send`` gets: the device's address and
    params for a device, the channel's config for a channel, nothing for the web."""
    if row.device_id is not None:
        return row.device_kind, {**(row.device_params or {}), "address": row.device_address}
    if row.has_channel:
        return row.channel_kind, dict(row.channel_config or {})
    return WEB, {}


def _refused(row, cls: type[Channel] | None, missing: str) -> str | None:
    """Why a locked delivery fails before its class is tried, or None: no class, a channel turned off, no grant on the
    project now, or the web session of its channel, or the token of its device, ended."""
    if cls is None:
        return missing
    if row.has_channel and not row.enabled:
        return "the channel was turned off before the notification went out"
    if (row.has_channel or row.device_id is not None) and not row.granted:
        return NO_GRANT
    if row.has_channel and (cls.needs_session or row.token_id is not None) and not row.session_live:
        return SESSION_ENDED
    if row.device_id is not None and not row.device_live:
        return push_store.SIGN_IN_ENDED
    return None


async def _gone(conn: AsyncConnection, row, reason: str) -> None:
    """The channel of a delivery turned off, or its device removed with its deliveries, for ``reason``."""
    if row.device_id is not None:
        removed = await push_store.remove_device(conn, row.device_id)
        if removed is not None:
            await audit.record(
                conn,
                actor_id=None,
                token_id=None,
                action=audit.PUSH_DEVICE_REMOVE,
                target=f"push_device:{removed[0]} kind={removed[1]} user={removed[2]} by=hub",
            )
    elif row.channel_id is not None:
        await conn.execute(_turn_off(row.channel_id, reason))


async def _instance(conn: AsyncConnection, delivery_id: int, kind: str, cls, instances: dict, config):
    """The pass's instance of ``cls`` for ``kind``, made once; None when it cannot be made (the delivery failed)."""
    channel = instances.get(kind)
    if channel is None:
        try:
            channel = instances[kind] = cls(config)
        except Exception as exc:  # a class that cannot be made fails its kind's deliveries, never the job
            await conn.execute(_failed(delivery_id, f"the class of channels of kind {kind} failed: {_error_text(exc)}"))
            log.warning("notification channel not made", extra={"delivery_id": delivery_id, "channel": kind})
    return channel


async def _not_sent(conn: AsyncConnection, delivery_id: int, row, kind: str, exc: Exception) -> str:
    """What becomes of a delivery whose class raised ``exc``: tried later, or failed (and its channel or device gone
    for ChannelGone)."""
    where = {"delivery_id": delivery_id, "channel": kind}
    windowed = _windowed(row)
    if windowed and row.past_window:  # tried DECISION_WINDOW since its first try: it fails with this error
        await conn.execute(_failed(delivery_id, _error_text(exc) or "the channel failed"))
        log.warning("notification delivery of a decision failed past its window", extra=where)
        return "failed"
    if isinstance(exc, RetryAfter):
        await conn.execute(_later(delivery_id, timedelta(seconds=exc.seconds), _error_text(exc), windowed))
        log.info("notification delivery waits as its channel asked", extra=where)
        return "retried"
    error = _error_text(exc) or "the channel failed"
    if isinstance(exc, Undeliverable):
        await conn.execute(_failed(delivery_id, error))
        if isinstance(exc, ChannelGone):
            await _gone(conn, row, str(exc) or error)
        log.warning("notification undeliverable", extra=where)
        return "failed"
    if row.attempts + 1 >= runs.MAX_DELIVERY_ATTEMPTS:
        await conn.execute(_failed(delivery_id, error))
        log.warning("notification delivery failed", extra=where)
        return "failed"
    await conn.execute(_retry(delivery_id, backoff(row.attempts + 1), error, windowed))
    log.info("notification delivery to try again", extra=where)
    return "retried"


def _windowed(row) -> bool:
    """Whether a locked delivery is tried within DECISION_WINDOW: a decision's, on a channel."""
    return bool(row.has_channel) and row.decision_id is not None


def _withheld(row, notification: Outgoing) -> str | None:
    """Why a channel's delivery went without the notification's content, as its ``prepare`` said, or None."""
    extra = notification.extra or {}
    if not row.has_channel or extra.get("restricted") is not True:
        return None
    said = extra.get("restricted_by")
    return said if said in WITHHELD else None


# Why a message went without its notification's content: notification_deliveries.withheld (0265)
WITHHELD = ("label", "default_label", "project")


async def _deliver_one(conn: AsyncConnection, delivery_id: int, instances: dict, config) -> str | None:
    """Try delivery ``delivery_id`` once, in the caller's transaction; delivered, retried or failed, or None when it
    is no longer due (taken by another pass, or done meanwhile) or quiet hours hold it."""
    row = (await conn.execute(_lock_due(delivery_id))).one_or_none()
    if row is None:
        return None
    found = row._mapping
    notification = Outgoing(**{name: found[name] for name in OUTGOING})
    kind, target = _target(row)
    cls, missing = _channel_class(kind)
    refused = _refused(row, cls, missing)
    if refused is None and row.device_id is not None and await push_tiers.closed(conn, row.decision_id):
        refused = DECISION_CLOSED
    if refused is not None:
        await conn.execute(_failed(delivery_id, refused))
        if refused in (SESSION_ENDED, push_store.SIGN_IN_ENDED):
            await _gone(conn, row, refused)
        log.info("notification not delivered", extra={"delivery_id": delivery_id, "channel": kind, "why": refused})
        return "failed"
    if row.device_id is not None:
        held = await push_tiers.held_until(conn, user_id=row.user_id, config=config)
        if held is not None:
            await conn.execute(_held(delivery_id, held))
            log.info("push held for quiet hours", extra={"delivery_id": delivery_id, "until": held.isoformat()})
            return None
    channel = await _instance(conn, delivery_id, kind, cls, instances, config)
    if channel is None:
        return "failed"
    token = SENDING.set(Sending(delivery_id, kind, row.edit_of, row.edit_external_id))
    try:
        async with conn.begin_nested():  # a statement of prepare that fails rolls back its savepoint alone
            notification = await channel.prepare(conn, notification)
        sent = await asyncio.wait_for(channel.send(notification, target), SEND_TIMEOUT_SECONDS)
    except Exception as exc:  # any failure of a channel is the delivery's, never the job's
        return await _not_sent(conn, delivery_id, row, kind, exc)
    finally:
        SENDING.reset(token)
    settled = not (row.has_channel and row.decision_open and row.edit_of is None)
    await conn.execute(_delivered(delivery_id, sent, withheld=_withheld(row, notification), settled=settled))
    if row.device_id is not None:
        await push_store.sent(conn, row.device_id)
    return "delivered"


async def queue_edits(conn: AsyncConnection, batch: int = EDIT_BATCH) -> int:
    """Give each channel's message of a decision that is no longer open (answered, cancelled or expired) its edit, in
    the caller's transaction: a delivery of the same notification on the same channel whose ``edit_of`` names the
    first, due now, and the first marked settled. The first deliveries are those delivered with the message's id and
    not settled; how many got an edit."""
    deliveries, stored, decisions = tables.notification_deliveries, tables.notifications, tables.decisions
    found = (
        select(deliveries.c.id, deliveries.c.notification_id, deliveries.c.channel_id)
        .join(stored, stored.c.id == deliveries.c.notification_id)
        .join(decisions, decisions.c.id == stored.c.decision_id)
        .where(
            deliveries.c.settled_at.is_(None),
            deliveries.c.edit_of.is_(None),
            deliveries.c.channel_id.is_not(None),
            deliveries.c.external_id.is_not(None),
            deliveries.c.state == "delivered",
            decisions.c.state != "open",
        )
        .order_by(deliveries.c.id)
        .limit(batch)
        .with_for_update(of=deliveries, skip_locked=True)
    )
    rows = (await conn.execute(found)).all()
    if not rows:
        return 0
    edits = [{"notification_id": row.notification_id, "channel_id": row.channel_id, "edit_of": row.id} for row in rows]
    await conn.execute(pg_insert(deliveries).values(edits).on_conflict_do_nothing(index_elements=["edit_of"]))
    ids = [row.id for row in rows]
    await conn.execute(update(deliveries).values(settled_at=func.now()).where(deliveries.c.id.in_(ids)))
    log.info("edits of channel messages queued", extra={"count": len(rows)})
    return len(rows)


async def decision_settled(conn: AsyncConnection) -> None:
    """Once ``conn``'s transaction, which answered or closed a decision, commits, a pass of the job runs, whose
    sweep queues the edits of the messages channels sent of it (``queue_edits``)."""
    await bus.soon(jobs.DELIVER_NOTIFICATIONS, conn)


async def _database_failed(engine, delivery_id: int, exc: SQLAlchemyError) -> str:
    """What becomes of delivery ``delivery_id`` whose transaction a database error ended: a failed try, in a
    transaction of its own, as a channel's failure is; failed when even that does not write."""
    where = {"delivery_id": delivery_id}
    log.warning("notification delivery hit a database error", extra=where, exc_info=exc)
    deliveries, error = tables.notification_deliveries, _error_text(exc)
    try:
        async with engine.begin() as conn:
            query = select(deliveries.c.attempts).where(deliveries.c.id == delivery_id, deliveries.c.state == "pending")
            attempts = (await conn.execute(query)).scalar_one_or_none()
            if attempts is None:
                return "failed"
            if attempts + 1 >= runs.MAX_DELIVERY_ATTEMPTS:
                await conn.execute(_failed(delivery_id, error))
                return "failed"
            await conn.execute(_retry(delivery_id, backoff(attempts + 1), error))
            return "retried"
    except SQLAlchemyError:
        log.exception("notification delivery not marked after a database error", extra=where)
        return "failed"


async def deliver_notifications(
    engine, *, config=None, batch: int = DELIVERY_BATCH, blobs: BlobStore | None = None
) -> dict:
    """One pass of the job hub.deliver_notifications: each pending delivery that is due, tried once in a transaction
    of its own, with ``blobs`` the store the files of notifications are presigned from (PASS_BLOBS). Returns how many
    were delivered, are to be tried again, and failed."""
    report = {"delivered": 0, "retried": 0, "failed": 0}
    async with engine.begin() as conn:
        await queue_edits(conn)
        due = (await conn.execute(_due(batch))).scalars().all()
    instances: dict[str, Channel] = {}
    token = PASS_BLOBS.set(blobs)
    try:
        for delivery_id in due:
            try:
                async with engine.begin() as conn:
                    outcome = await _deliver_one(conn, delivery_id, instances, config)
            except SQLAlchemyError as exc:  # a database error is this delivery's: the others of the pass go on
                outcome = await _database_failed(engine, delivery_id, exc)
            if outcome is not None:
                report[outcome] += 1
    finally:
        PASS_BLOBS.reset(token)
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
    notice_kind: str | None = Field(
        description="a notice's kind: one of the hub's, or one a core plugin declares (contributes.notice_kinds, its "
        "table prefix first); null for a decision"
    )
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


def report_notice(stored=None):
    """The SQL of ``runs.report_only`` over ``stored`` (tables.notifications when None)."""
    stored = tables.notifications if stored is None else stored
    return and_(
        stored.c.kind == "notice",
        stored.c.notice_kind.in_(runs.REPORT_NOTICE_KINDS),
        stored.c.details[runs.REPORT_ASK].astext.is_(None),
    )


def shown():
    """Notifications with their project's name, their decision's state and their proposal's: the FROM of the member's
    routes."""
    stored, projects, decisions, proposals = tables.notifications, tables.projects, tables.decisions, tables.proposals
    return (
        stored.outerjoin(projects, projects.c.id == stored.c.project_id)
        .outerjoin(decisions, decisions.c.id == stored.c.decision_id)
        .outerjoin(proposals, proposals.c.id == stored.c.proposal_id)
    )


def notification_rows():
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
        func.coalesce(report_notice(stored), false()).label("report"),
    ).select_from(shown())


def notifications_of(rows) -> list[Notification]:
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
        stored = notification_rows().where(tables.notifications.c.id == notification_id)
        (view,) = notifications_of((await conn.execute(stored)).all())
    log.info("notice sent", extra={"run_id": run_id, "kind": body.kind, "plan_id": plan_id})
    return view
