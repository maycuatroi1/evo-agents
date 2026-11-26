"""The outbox in SQL: a notification stored with its deliveries (``store``, ``add_deliveries``), the deliveries due
(``due``), one locked with all a pass needs to judge it (``lock_due``), and the moves of a delivery: delivered, tried
later, held for quiet hours, retried, failed, and the channel turned off. ``queue_edits`` gives each channel's message
of a decision no longer open its edit."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import (
    BigInteger,
    and_,
    bindparam,
    exists,
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
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.notifications.models import (
    DECISION_WINDOW,
    MAX_ERROR_CHARS,
    MAX_EXTERNAL_ID_CHARS,
    WEB_SESSION,
)
from evo_agents.hub.server.push import repository as push_repository


async def store(
    conn: AsyncConnection,
    *,
    user_id: int,
    kind: str,
    notice_kind: str | None,
    project_id: int | None,
    run_id: int | None,
    decision_id: int | None,
    proposal_id: int | None,
    title: str,
    body: str | None,
    details: dict | None,
    link: str | None,
    report: bool,
) -> int:
    """Store one notification, read already when it only ``report``s; its id."""
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
            title=title,
            body=body,
            details=details if details is not None else null(),  # SQL NULL: None alone would store JSON null
            link=link,
            read_at=func.now() if report else null(),
        )
        .returning(stored.c.id)
    )
    return inserted.scalar_one()


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


async def add_deliveries(conn: AsyncConnection, notification_id: int, user_id: int, *, web_only: bool) -> None:
    """The deliveries of ``_deliveries``, stored."""
    await conn.execute(_deliveries(notification_id, user_id, web_only=web_only))


async def due(conn: AsyncConnection, batch: int) -> list[int]:
    """The first ``batch`` pending deliveries whose ``next_at`` has passed, the longest due first."""
    deliveries = tables.notification_deliveries
    query = (
        select(deliveries.c.id)
        .where(deliveries.c.state == "pending", deliveries.c.next_at <= func.now())
        .order_by(deliveries.c.next_at, deliveries.c.id)
        .limit(batch)
    )
    return list((await conn.execute(query)).scalars().all())


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
            push_repository.device_live(devices).label("device_live"),
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


async def lock_due(conn: AsyncConnection, delivery_id: int):
    """The row of ``_lock_due``, or None."""
    return (await conn.execute(_lock_due(delivery_id))).one_or_none()


async def delivered(
    conn: AsyncConnection,
    delivery_id: int,
    external_id: str | None = None,
    *,
    withheld: str | None = None,
    settled: bool = True,
) -> None:
    """Delivery ``delivery_id`` delivered as ``external_id``, sent without the notification's content for
    ``withheld``; ``settled`` unless its message may need an edit later (a decision still open)."""
    deliveries = tables.notification_deliveries
    await conn.execute(
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


async def later(conn: AsyncConnection, delivery_id: int, wait: timedelta, error: str, windowed: bool = False) -> None:
    """Try delivery ``delivery_id`` again ``wait`` from now, as its service asked, without counting a failed try."""
    deliveries = tables.notification_deliveries
    await conn.execute(
        update(deliveries)
        .values(next_at=_next_at(wait, windowed), last_error=error)
        .where(deliveries.c.id == delivery_id)
    )


async def held(conn: AsyncConnection, delivery_id: int, until: datetime) -> None:
    """Delivery ``delivery_id`` held until ``until`` by its member's quiet hours, without counting a try."""
    deliveries = tables.notification_deliveries
    await conn.execute(update(deliveries).values(next_at=until).where(deliveries.c.id == delivery_id))


async def turn_off(conn: AsyncConnection, channel_id: int, reason: str) -> None:
    """Channel ``channel_id`` off, with ``reason`` and the time in its config, as the member's settings show it."""
    channels = tables.notification_channels
    said = {"disabled_reason": reason[:MAX_ERROR_CHARS]}
    await conn.execute(
        update(channels)
        .values(
            enabled=False,
            config=channels.c.config.op("||")(bindparam("said", said, type_=JSONB)).op("||")(
                func.jsonb_build_object("disabled_at", func.now())
            ),
        )
        .where(channels.c.id == channel_id)
    )


async def retry(conn: AsyncConnection, delivery_id: int, wait: timedelta, error: str, windowed: bool = False) -> None:
    """A failed try of delivery ``delivery_id``, tried again ``wait`` from now."""
    deliveries = tables.notification_deliveries
    await conn.execute(
        update(deliveries)
        .values(attempts=deliveries.c.attempts + 1, next_at=_next_at(wait, windowed), last_error=error)
        .where(deliveries.c.id == delivery_id)
    )


async def failed(conn: AsyncConnection, delivery_id: int, error: str) -> None:
    """Delivery ``delivery_id`` failed with ``error``."""
    deliveries = tables.notification_deliveries
    await conn.execute(
        update(deliveries)
        .values(attempts=deliveries.c.attempts + 1, state="failed", last_error=error)
        .where(deliveries.c.id == delivery_id)
    )


async def pending_attempts(conn: AsyncConnection, delivery_id: int) -> int | None:
    """The failed tries of delivery ``delivery_id`` while it is pending, else None."""
    deliveries = tables.notification_deliveries
    query = select(deliveries.c.attempts).where(deliveries.c.id == delivery_id, deliveries.c.state == "pending")
    return (await conn.execute(query)).scalar_one_or_none()


async def queue_edits(conn: AsyncConnection, batch: int) -> int:
    """The edits of ``notifications.service.deliveries.queue_edits`` stored, and the first deliveries marked settled;
    how many got an edit."""
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
    return len(rows)
