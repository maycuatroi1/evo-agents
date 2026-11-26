"""The job hub.deliver_notifications: the outbox sent out. ``docs/notifications.md`` is the design and ``runs`` holds
the kinds and limits.

``deliver_notifications`` is the job hub.deliver_notifications, every minute, and queued by each transaction that
stores a notification (``bus.soon``), so it goes out once that commits; with the event bus on, the delivery process
(``evo-agents hub deliver``) runs that pass instead, as soon as the broker carries the news of the commit. It takes the
pending deliveries whose ``next_at`` has passed, each in a transaction of its own with ``FOR UPDATE SKIP LOCKED``, and
hands each to an instance of the class CHANNELS names for its channel's kind (WEB for a delivery without a channel).
The web's class sends nothing, since the web reads notifications from the table, so its deliveries are delivered at
once. A class that raises leaves the delivery pending with ``last_error`` and a ``next_at`` BACKOFF_SECONDS later,
doubling from one try to the next up to BACKOFF_MAX_SECONDS, and its MAX_DELIVERY_ATTEMPTS-th failure marks it
failed. Three exceptions say more: RetryAfter (the service asked to wait, such as Telegram's 429 with retry_after)
moves ``next_at`` that far without counting a failed try; Undeliverable fails the delivery at once; ChannelGone fails
it and turns the channel off with its reason in the channel's config (``disabled_reason``), as when a member blocked
the bot. A kind with no class in CHANNELS, or a channel turned off since, fails its delivery at once with that reason,
so a hub that drops a channel does not try it forever. So does a kind whose class a core plugin contributes but does
not load, or does not make (its import or its ``__init__`` raises): ``last_error`` says why, and the deliveries of
every other kind, the web's included, go on. Adding a channel takes a subclass of Channel, contributed by a core
plugin to the channels under the kind's name (as the core plugin telegram does), or an entry in CHANNELS
(``channels``); the tables, the outbox and the job stay as they are. A class may read more of a notification first
(``prepare``, in a savepoint of the delivery's transaction), and ``send`` may answer the id the service gave the
message, kept as the delivery's ``external_id``. A database error in one delivery is a failed try of that delivery
alone: the others of the pass go on.

A delivery of a decision on a channel, and the edit of its message, is tried for DECISION_WINDOW from when it was
stored, which is its first try since the job runs once the transaction commits (question #99): its retries keep to
RetryAfter or the backoff above, each ``next_at`` no later than the end of the window, and a try that fails at or
after that end fails the delivery with its error, RetryAfter included. The decision stays open on the hub, and its
view says the channel failed (``decisions.service.channels``). While a class's ``prepare`` runs, SENDING holds the
delivery it prepares for: its id, the kind of its channel, and for an edit the delivery it edits and that message's
id, which ``member_channels.prepare`` reads to judge a decision by its label against the channel's sink and to tell
the class to edit. A delivery sent without the notification's content (``extra["restricted"]``) keeps why in
``withheld``.

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
marks when the device last got a push. The member's quiet hours may hold a push (``tiers.held_until``): its
``next_at`` moves to when it may go, without counting a try, and the pass counts it nowhere. A push of a decision that
is no longer open fails with DECISION_CLOSED instead of asking what nobody asks any more.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import bus, jobs, runs
from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.server import audit
from evo_agents.hub.server.notifications.models import (
    BACKOFF_MAX_SECONDS,
    BACKOFF_SECONDS,
    DECISION_CLOSED,
    DELIVERY_BATCH,
    EDIT_BATCH,
    NO_GRANT,
    OUTGOING,
    SEND_TIMEOUT_SECONDS,
    SESSION_ENDED,
    WEB,
    WITHHELD,
    ChannelGone,
    Outgoing,
    RetryAfter,
    Sending,
    Undeliverable,
)
from evo_agents.hub.server.notifications.repository import outbox
from evo_agents.hub.server.notifications.service.channels import (
    PASS_BLOBS,
    SENDING,
    Channel,
    error_text,
    loaded_class,
)

# The Notifier, for hub.kg_build: the import-linter exception of its notice names this module, notifications.py's path
from evo_agents.hub.server.notifications.service.notifier import NOTIFIER as NOTIFIER
from evo_agents.hub.server.push import repository as push_repository
from evo_agents.hub.server.push.models import SIGN_IN_ENDED
from evo_agents.hub.server.push.service import tiers

log = logging.getLogger(__name__)


def backoff(attempts: int) -> timedelta:
    """How long a delivery waits after its ``attempts``-th failed try."""
    return timedelta(seconds=min(BACKOFF_SECONDS * 2 ** max(0, attempts - 1), BACKOFF_MAX_SECONDS))


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
        return SIGN_IN_ENDED
    return None


async def _gone(conn: AsyncConnection, row, reason: str) -> None:
    """The channel of a delivery turned off, or its device removed with its deliveries, for ``reason``."""
    if row.device_id is not None:
        removed = await push_repository.remove_device(conn, row.device_id)
        if removed is not None:
            await audit.record(
                conn,
                actor_id=None,
                token_id=None,
                action=audit.PUSH_DEVICE_REMOVE,
                target=f"push_device:{removed[0]} kind={removed[1]} user={removed[2]} by=hub",
            )
    elif row.channel_id is not None:
        await outbox.turn_off(conn, row.channel_id, reason)


async def _instance(conn: AsyncConnection, delivery_id: int, kind: str, cls, instances: dict, config):
    """The pass's instance of ``cls`` for ``kind``, made once; None when it cannot be made (the delivery failed)."""
    channel = instances.get(kind)
    if channel is None:
        try:
            channel = instances[kind] = cls(config)
        except Exception as exc:  # a class that cannot be made fails its kind's deliveries, never the job
            await outbox.failed(conn, delivery_id, f"the class of channels of kind {kind} failed: {error_text(exc)}")
            log.warning("notification channel not made", extra={"delivery_id": delivery_id, "channel": kind})
    return channel


async def _not_sent(conn: AsyncConnection, delivery_id: int, row, kind: str, exc: Exception) -> str:
    """What becomes of a delivery whose class raised ``exc``: tried later, or failed (and its channel or device gone
    for ChannelGone)."""
    where = {"delivery_id": delivery_id, "channel": kind}
    windowed = _windowed(row)
    if windowed and row.past_window:  # tried DECISION_WINDOW since its first try: it fails with this error
        await outbox.failed(conn, delivery_id, error_text(exc) or "the channel failed")
        log.warning("notification delivery of a decision failed past its window", extra=where)
        return "failed"
    if isinstance(exc, RetryAfter):
        await outbox.later(conn, delivery_id, timedelta(seconds=exc.seconds), error_text(exc), windowed)
        log.info("notification delivery waits as its channel asked", extra=where)
        return "retried"
    error = error_text(exc) or "the channel failed"
    if isinstance(exc, Undeliverable):
        await outbox.failed(conn, delivery_id, error)
        if isinstance(exc, ChannelGone):
            await _gone(conn, row, str(exc) or error)
        log.warning("notification undeliverable", extra=where)
        return "failed"
    if row.attempts + 1 >= runs.MAX_DELIVERY_ATTEMPTS:
        await outbox.failed(conn, delivery_id, error)
        log.warning("notification delivery failed", extra=where)
        return "failed"
    await outbox.retry(conn, delivery_id, backoff(row.attempts + 1), error, windowed)
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


async def _deliver_one(conn: AsyncConnection, delivery_id: int, instances: dict, config) -> str | None:
    """Try delivery ``delivery_id`` once, in the caller's transaction; delivered, retried or failed, or None when it
    is no longer due (taken by another pass, or done meanwhile) or quiet hours hold it."""
    row = await outbox.lock_due(conn, delivery_id)
    if row is None:
        return None
    found = row._mapping
    notification = Outgoing(**{name: found[name] for name in OUTGOING})
    kind, target = _target(row)
    cls, missing = loaded_class(kind)
    refused = _refused(row, cls, missing)
    if refused is None and row.device_id is not None and await tiers.closed(conn, row.decision_id):
        refused = DECISION_CLOSED
    if refused is not None:
        await outbox.failed(conn, delivery_id, refused)
        if refused in (SESSION_ENDED, SIGN_IN_ENDED):
            await _gone(conn, row, refused)
        log.info("notification not delivered", extra={"delivery_id": delivery_id, "channel": kind, "why": refused})
        return "failed"
    if row.device_id is not None:
        held = await tiers.held_until(conn, user_id=row.user_id, config=config)
        if held is not None:
            await outbox.held(conn, delivery_id, held)
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
    await outbox.delivered(conn, delivery_id, sent, withheld=_withheld(row, notification), settled=settled)
    if row.device_id is not None:
        await push_repository.sent(conn, row.device_id)
    return "delivered"


async def queue_edits(conn: AsyncConnection, batch: int = EDIT_BATCH) -> int:
    """Give each channel's message of a decision that is no longer open (answered, cancelled or expired) its edit, in
    the caller's transaction: a delivery of the same notification on the same channel whose ``edit_of`` names the
    first, due now, and the first marked settled. The first deliveries are those delivered with the message's id and
    not settled; how many got an edit."""
    queued = await outbox.queue_edits(conn, batch)
    if queued:
        log.info("edits of channel messages queued", extra={"count": queued})
    return queued


async def decision_settled(conn: AsyncConnection) -> None:
    """Once ``conn``'s transaction, which answered or closed a decision, commits, a pass of the job runs, whose
    sweep queues the edits of the messages channels sent of it (``queue_edits``)."""
    await bus.soon(jobs.DELIVER_NOTIFICATIONS, conn)


async def _database_failed(engine, delivery_id: int, exc: SQLAlchemyError) -> str:
    """What becomes of delivery ``delivery_id`` whose transaction a database error ended: a failed try, in a
    transaction of its own, as a channel's failure is; failed when even that does not write."""
    where = {"delivery_id": delivery_id}
    log.warning("notification delivery hit a database error", extra=where, exc_info=exc)
    error = error_text(exc)
    try:
        async with engine.begin() as conn:
            attempts = await outbox.pending_attempts(conn, delivery_id)
            if attempts is None:
                return "failed"
            if attempts + 1 >= runs.MAX_DELIVERY_ATTEMPTS:
                await outbox.failed(conn, delivery_id, error)
                return "failed"
            await outbox.retry(conn, delivery_id, backoff(attempts + 1), error)
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
        due = await outbox.due(conn, batch)
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
