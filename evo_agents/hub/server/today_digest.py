"""The summary push of Today (plan hub-mobile-phase-0, criterion a7): the job hub.today_digest, every minute, which
``brief.send_today_digests`` runs, as the Curator's morning brief (``curator.brief``) is the pattern of a job at a local
time, once a day.

A pass reads the members who may get one now: those with a push device that reaches them (``push_store.device_live``),
away from the hub for at least AWAY, by ``users.last_seen_at`` and by their seen mark of Today alike (the hub writes
``last_seen_at`` at most once a day a token, ``security.TOUCH_EVERY``, so a member who looked at Today within AWAY is
not away), and who did not set the tier of kind ``today`` to off (``push_tiers``). For each, in a transaction of its
own, it takes the member's local time in their time zone (EVO_HUB_WEB_TIME_ZONE until they choose one; UTC, logged,
for a zone Postgres no longer knows). From their ``digest_at`` (DEFAULT_DIGEST_AT until they choose one) until
``curator.BRIEF_GRACE`` later, on a local day that has none yet (``today_marks.digest_on``, claimed before Today is
worked out, so a second pass or a second hub worker sends nothing), it works out the member's Today
(``today.compute``). When it has an item, the member gets one notification of the notice ``today``: no project, its
title TITLE, its body Today's sentence, its link Home, read already, delivered to their push devices alone, so the
Inbox keeps a record of it and no channel repeats it. The quiet hours of the member hold it as any push
(``push_tiers.held_until``). A day whose Today has no item ends with no push, and its claim keeps the pass from working
it out again until the next day.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timedelta

from sqlalchemy import DateTime, exists, func, insert, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import curator, runs, tables
from evo_agents.hub.config import DEFAULT_WEB_TIME_ZONE
from evo_agents.hub.server import push_store, push_tiers, today
from evo_agents.hub.server.security import MACHINE, Principal
from evo_agents.hub.tables_push import DEFAULT_DIGEST_AT, OFF

log = logging.getLogger(__name__)

AWAY = timedelta(hours=8)  # a member seen on the hub more recently gets no summary push
NOTICE = "today"  # runs.NOTICE_KINDS: the summary push
TITLE = "Today on the hub"
HOME = "/"  # the web's Home, which opens with Today


def _candidates(now: datetime):
    """The members who may get a summary push at ``now`` (the module): their id, login, time zone, digest_at and the
    local day of their last summary push."""
    u, p, t, d = tables.users, tables.notification_preferences, tables.today_marks, tables.push_devices
    away = literal(now, DateTime(timezone=True)) - AWAY
    device = exists().where(d.c.user_id == u.c.id, push_store.device_live(d))
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


async def _local_now(conn: AsyncConnection, zone: str | None, now: datetime, config, zones: dict) -> datetime:
    """The local time at ``now`` in ``zone`` (the hub's web time zone when None), known zones remembered in
    ``zones`` for the pass."""
    name = zone or getattr(config, "web_time_zone", None) or DEFAULT_WEB_TIME_ZONE
    if name not in zones:
        known = name
        if not await push_tiers.time_zone_known(conn, name):
            log.warning("summary push in a time zone Postgres does not know", extra={"zone": name})
            known = push_tiers.FALLBACK_ZONE
        moment = literal(now, DateTime(timezone=True))
        zones[name] = (await conn.execute(select(func.timezone(known, moment)))).scalar_one()
    return zones[name]


async def _claim(conn: AsyncConnection, user_id: int, day: date) -> bool:
    """Take local day ``day`` for member ``user_id``'s summary push; False when it was taken already."""
    t = tables.today_marks
    claimed = pg_insert(t).values(user_id=user_id, digest_on=day)
    claimed = claimed.on_conflict_do_update(
        constraint="today_marks_pkey",
        set_={"digest_on": day, "updated_at": func.now()},
        where=or_(t.c.digest_on.is_(None), t.c.digest_on != day),
    )
    return (await conn.execute(claimed.returning(t.c.user_id))).first() is not None


async def _push(conn: AsyncConnection, user_id: int, found: today.Today) -> int:
    """The summary push of ``found`` to member ``user_id``: its notification, read already, and a delivery on each of
    their devices that reaches them; its id."""
    n = tables.notifications
    details = {"items": len(found.items), "needs_you": found.needs_you_count, "caught_up": found.caught_up}
    stored = insert(n).values(
        user_id=user_id,
        kind="notice",
        notice_kind=NOTICE,
        title=TITLE,
        body=found.summary,
        details=details,
        link=HOME,
        read_at=func.now(),
    )
    notification_id = (await conn.execute(stored.returning(n.c.id))).scalar_one()
    await conn.execute(push_store.device_deliveries(notification_id, user_id))
    return notification_id


async def _digest(conn: AsyncConnection, row, now: datetime, config, zones: dict) -> str:
    """The summary push of member ``row`` at ``now``, in the caller's transaction; a word for the job's summary."""
    local_now = await _local_now(conn, row.time_zone, now, config, zones)
    if not curator.brief_due(local_now, row.digest_at):
        return "not_due"
    day = local_now.date()
    if row.digest_on == day or not await _claim(conn, row.id, day):
        return "sent"
    member = Principal(user_id=row.id, login=row.login, admin=False, token_id=0, kind=MACHINE, token_hash="")
    wait = timedelta(seconds=getattr(config, "decision_wait_seconds", None) or runs.DECISION_WAIT_SECONDS)
    found = await today.compute(conn, member, wait, now=now)
    if not found.items:
        return "caught_up"
    notification_id = await _push(conn, row.id, found)
    log.info(
        "summary push sent",
        extra={"login": row.login, "day": day.isoformat(), "items": len(found.items), "id": notification_id},
    )
    return "today"


async def send(engine: AsyncEngine, *, config=None, now: datetime | None = None) -> dict:
    """One pass of ``hub.today_digest`` at ``now`` (the database's now when None), each member in a transaction of
    its own; how many members ended in each outcome."""
    async with engine.begin() as conn:
        if now is None:
            now = (await conn.execute(select(func.now()))).scalar_one()
        found = (await conn.execute(_candidates(now))).all()
    outcomes: Counter = Counter()
    zones: dict[str, datetime] = {}
    for row in found:
        try:
            async with engine.begin() as conn:
                outcomes[await _digest(conn, row, now, config, zones)] += 1
        except Exception:  # one member failing leaves the others their push
            log.exception("a summary push failed", extra={"login": row.login})
            outcomes["failed"] += 1
    return dict(sorted(outcomes.items()))
