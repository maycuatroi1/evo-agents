"""What the summary push of Today reads and writes: the members who may get one now, the local time in a zone, the
claim of a local day (today_marks) and the notification with its deliveries."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import DateTime, exists, func, insert, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.digests.models.today import AWAY, NOTICE, TITLE, TODAY
from evo_agents.hub.server.push import repository as push_repository
from evo_agents.hub.tables_push import DEFAULT_DIGEST_AT, OFF


async def now(conn: AsyncConnection) -> datetime:
    """The database's now."""
    return (await conn.execute(select(func.now()))).scalar_one()


def _candidates(at: datetime):
    """The members who may get a summary push at ``at`` (``digests.service.today``): their id, login, time zone,
    digest_at and the local day of their last summary push."""
    u, p, t, d = tables.users, tables.notification_preferences, tables.today_marks, tables.push_devices
    away = literal(at, DateTime(timezone=True)) - AWAY
    device = exists().where(d.c.user_id == u.c.id, push_repository.device_live(d))
    tier = func.coalesce(p.c.tiers[NOTICE].astext, "")
    return (
        select(
            u.c.id,
            u.c.login,
            p.c.time_zone,
            func.coalesce(p.c.digest_at, DEFAULT_DIGEST_AT).label("digest_at"),
            t.c.digest_on,
        )
        .select_from(u.outerjoin(p, p.c.user_id == u.c.id).outerjoin(t, t.c.user_id == u.c.id))
        .where(
            device,
            or_(u.c.last_seen_at.is_(None), u.c.last_seen_at <= away),
            or_(t.c.seen_at.is_(None), t.c.seen_at <= away),
            tier != OFF,
        )
        .order_by(u.c.id)
    )


async def candidates(conn: AsyncConnection, at: datetime) -> list:
    return list((await conn.execute(_candidates(at))).all())


async def local_time(conn: AsyncConnection, zone: str, at: datetime) -> datetime:
    """The local time at ``at`` in ``zone``, which Postgres knows."""
    moment = literal(at, DateTime(timezone=True))
    return (await conn.execute(select(func.timezone(zone, moment)))).scalar_one()


async def claim(conn: AsyncConnection, user_id: int, day: date) -> bool:
    """Take local day ``day`` for member ``user_id``'s summary push; False when it was taken already."""
    t = tables.today_marks
    claimed = pg_insert(t).values(user_id=user_id, digest_on=day)
    claimed = claimed.on_conflict_do_update(
        constraint="today_marks_pkey",
        set_={"digest_on": day, "updated_at": func.now()},
        where=or_(t.c.digest_on.is_(None), t.c.digest_on != day),
    )
    return (await conn.execute(claimed.returning(t.c.user_id))).first() is not None


async def pushed(conn: AsyncConnection, user_id: int, body: str, details: dict) -> int:
    """The notification of a summary push to member ``user_id``, read already, and a delivery on each of their devices
    that reaches them; its id."""
    n = tables.notifications
    stored = insert(n).values(
        user_id=user_id,
        kind="notice",
        notice_kind=NOTICE,
        title=TITLE,
        body=body,
        details=details,
        link=TODAY,
        read_at=func.now(),
    )
    notification_id = (await conn.execute(stored.returning(n.c.id))).scalar_one()
    await conn.execute(push_repository.device_deliveries(notification_id, user_id))
    return notification_id
