"""The tiers of notifications and the quiet hours of members (plan hub-mobile-phase-0, criterion a3), in SQLAlchemy
Core on notification_preferences (schema 0157). ``docs/notifications.md`` is the design; ``output_notices`` has the
routes that read and set them (GET and PUT /v1/me/notification-settings).

**Tiers.** Each kind of notification (KINDS: decision, proposal and each kind of notice) is of one tier for a member:
interrupt, pushed to their devices; brief, only in Today and the Inbox; or off, in the Inbox alone. The tier decides
push alone: every notification still goes to the Inbox, the web's bell and the member's channels as before, and a
notice the member turned off (``output_notices.muted``) still is not sent at all. ``interrupts`` tells ``notifications
.notify`` whether a notification gets a delivery on each of its member's devices.

A tier the member chose for a kind (``tiers``) holds for every notification of that kind. Without one, the hub's
default (``default_tier``) holds: interrupt for a decision asked of the member (the person who answers it,
``decisions.asked_person``), for a run_failed of a run they dispatched (not one of CURATOR_KINDS, which the night shift
queues on its own), for a release notice of a release that went wrong (any event but ``published``) and for
merge_paused, the breaker of a merge queue that stopped a deploy; brief for every other notification, those of a kind
added later included. DEFAULT_INTERRUPTS are the kinds whose default is interrupt for some of their notifications.

**Quiet hours.** A member may set quiet hours, from ``quiet_start`` to ``quiet_end`` (HH:MM, past midnight when the end
is not after the start), in their time zone (an IANA name, the hub's EVO_HUB_WEB_TIME_ZONE until they choose one).
The job hub.deliver_notifications asks ``held_until`` before it pushes to a device: inside the quiet hours a push waits
until they end, unless it is of a decision whose run parks for want of an answer within PARK_SOON, which goes at once;
one whose run parks later waits at most until PARK_SOON before the park. A run parks EVO_HUB_DECISION_WAIT_SECONDS
after it starts waiting (a chat run sooner, ``runs.CHAT_DECISION_WAIT_SECONDS``); a run that does not wait yet would
park that long from now, and a local run, or one parked or ended already, parks no more. The check runs at the
delivery, not when the notification is stored, so quiet hours set or changed meanwhile hold. A push of a decision that
is no longer open by then is not sent (``closed``).

``digest_at`` is the local time of the member's summary push (DEFAULT_DIGEST_AT until they choose one), which the job
of plan hub-mobile-phase-0 step 5 reads with ``preferences``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import DateTime, Text, column, exists, func, literal, select, table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import curator, runs, tables
from evo_agents.hub.config import DEFAULT_WEB_TIME_ZONE
from evo_agents.hub.tables_push import BRIEF, INTERRUPT, TIERS

log = logging.getLogger(__name__)

KINDS = ("decision", "proposal", *runs.NOTICE_KINDS)  # the kinds of notification a member sets a tier for
# The kinds whose default tier is interrupt, for the notifications ``default_tier`` names: the rest default to brief.
DEFAULT_INTERRUPTS = ("decision", "run_failed", "release", "merge_paused")
RELEASED = "published"  # the event of a release notice of a release that went out (``release_watch``)
PARK_SOON = timedelta(hours=2)  # a decision whose run parks this soon is pushed inside quiet hours too
FALLBACK_ZONE = "UTC"  # the time zone of quiet hours whose own Postgres no longer knows (logged)
PG_TIMEZONES = table("pg_timezone_names", column("name", Text))  # the zones Postgres knows
PREFERENCES = ("tiers", "quiet_start", "quiet_end", "time_zone", "digest_at")  # the columns ``set_preferences`` sets


def kind_of(kind: str, notice_kind: str | None) -> str:
    """The kind a notification's tier is chosen for: its notice's kind for a notice, else its own."""
    return notice_kind if kind == "notice" and notice_kind else kind


def kind_default(kind: str) -> str:
    """The tier the hub gives ``kind`` until a member chooses one, as the settings show it: interrupt for the kinds of
    DEFAULT_INTERRUPTS (for the notifications ``default_tier`` names), brief for every other."""
    return INTERRUPT if kind in DEFAULT_INTERRUPTS else BRIEF


@dataclass(frozen=True)
class Facts:
    """What the hub's default tier of one notification to one member depends on."""

    kind: str  # kind_of the notification
    asked_of_member: bool = False  # a decision the member answers
    dispatched_by_member: bool = False  # the notification's run was dispatched by the member
    run_kind: str | None = None
    event: str | None = None  # details["event"] of a notice


def default_tier(facts: Facts) -> str:
    """The hub's tier for one notification to a member who chose none for its kind (the module)."""
    if facts.kind == "decision":
        return INTERRUPT if facts.asked_of_member else BRIEF
    if facts.kind == "run_failed":
        mine = facts.dispatched_by_member and facts.run_kind not in runs.CURATOR_KINDS
        return INTERRUPT if mine else BRIEF
    if facts.kind == "release":
        return BRIEF if facts.event == RELEASED else INTERRUPT
    if facts.kind == "merge_paused":
        return INTERRUPT
    return BRIEF


async def preferences(conn: AsyncConnection, user_id: int):
    """Member ``user_id``'s row of notification_preferences, or None when they chose nothing yet."""
    p = tables.notification_preferences
    query = select(p.c.hide_details, *(p.c[name] for name in PREFERENCES)).where(p.c.user_id == user_id)
    return (await conn.execute(query)).one_or_none()


def chosen_tiers(row) -> dict[str, str]:
    """The tiers a member chose (their preferences' ``tiers``), each a kind of KINDS and one of TIERS."""
    found = row.tiers if row is not None and isinstance(row.tiers, dict) else {}
    return {kind: tier for kind, tier in found.items() if kind in KINDS and tier in TIERS}


async def _facts(conn: AsyncConnection, kind: str, user_id: int, run_id, decision_id, details) -> Facts:
    if kind == "decision" and decision_id is not None:
        from evo_agents.hub.server.decisions import asked_person  # decisions imports notifications, which imports this

        d, r = tables.decisions, tables.runs
        asked = select(asked_person()).select_from(d.join(r, r.c.id == d.c.run_id)).where(d.c.id == decision_id)
        return Facts(kind, asked_of_member=(await conn.execute(asked)).scalar_one_or_none() == user_id)
    if kind == "run_failed" and run_id is not None:
        r = tables.runs
        row = (await conn.execute(select(r.c.dispatched_by, r.c.kind).where(r.c.id == run_id))).one_or_none()
        if row is not None:
            return Facts(kind, dispatched_by_member=row.dispatched_by == user_id, run_kind=row.kind)
    event = details.get("event") if isinstance(details, dict) else None
    return Facts(kind, event=event if isinstance(event, str) else None)


async def interrupts(
    conn: AsyncConnection,
    *,
    user_id: int,
    kind: str,
    notice_kind: str | None = None,
    run_id: int | None = None,
    decision_id: int | None = None,
    details: dict | None = None,
) -> bool:
    """Whether a notification of ``kind`` (and ``notice_kind``) to member ``user_id`` is of the interrupt tier for
    them, so it is pushed to their devices (the module)."""
    key = kind_of(kind, notice_kind)
    chosen = chosen_tiers(await preferences(conn, user_id)).get(key)
    if chosen is not None:
        return chosen == INTERRUPT
    if key not in DEFAULT_INTERRUPTS:
        return False
    return default_tier(await _facts(conn, key, user_id, run_id, decision_id, details)) == INTERRUPT


# Quiet hours


async def time_zone_known(conn: AsyncConnection, name: str) -> bool:
    """Whether Postgres knows the time zone ``name``, as the Curator's charter checks its own."""
    return bool((await conn.execute(select(exists().where(PG_TIMEZONES.c.name == name)))).scalar_one())


async def _parks_at(conn: AsyncConnection, decision_id: int, now: datetime, wait: timedelta) -> datetime | None:
    """When the run of decision ``decision_id`` parks for want of an answer (the module), or None when it never will."""
    d, r = tables.decisions, tables.runs
    query = select(r.c.kind, r.c.state, r.c.waiting_since).select_from(d.join(r, r.c.id == d.c.run_id))
    run = (await conn.execute(query.where(d.c.id == decision_id))).one_or_none()
    if run is None or run.kind == runs.LOCAL_KIND:
        return None
    if run.kind == "chat":
        wait = min(wait, timedelta(seconds=runs.CHAT_DECISION_WAIT_SECONDS))
    if run.state == "waiting" and run.waiting_since is not None:
        return run.waiting_since + wait
    if run.state in ("queued", *runs.CLOCK_STATES):
        return now + wait
    return None


async def held_until(conn: AsyncConnection, *, user_id: int, decision_id: int | None, config=None) -> datetime | None:
    """Until when member ``user_id``'s quiet hours hold a push to their devices of a notification of decision
    ``decision_id`` (None for any other), on a hub configured with ``config``; None when it goes now (the module)."""
    row = await preferences(conn, user_id)
    if row is None or row.quiet_start is None or row.quiet_end is None:
        return None
    zone = row.time_zone or getattr(config, "web_time_zone", None) or DEFAULT_WEB_TIME_ZONE
    if not await time_zone_known(conn, zone):
        log.warning("quiet hours in a time zone Postgres does not know", extra={"user_id": user_id, "zone": zone})
        zone = FALLBACK_ZONE
    now, local_now = (await conn.execute(select(func.now(), func.timezone(zone, func.now())))).one()
    start, end = curator.parse_time(row.quiet_start), curator.parse_time(row.quiet_end)
    if not curator.in_window(local_now.time(), start, end):
        return None
    local_end = datetime.combine(local_now.date(), end)
    if local_end <= local_now:
        local_end += timedelta(days=1)
    ends = (await conn.execute(select(func.timezone(zone, literal(local_end, DateTime()))))).scalar_one()
    if decision_id is None:
        return ends
    wait = timedelta(seconds=getattr(config, "decision_wait_seconds", None) or runs.DECISION_WAIT_SECONDS)
    parks = await _parks_at(conn, decision_id, now, wait)
    if parks is None:
        return ends
    if parks - now <= PARK_SOON:
        return None
    return min(ends, parks - PARK_SOON)


async def closed(conn: AsyncConnection, decision_id: int | None) -> bool:
    """Whether decision ``decision_id`` is no longer open, so a push of it would ask what nobody asks any more."""
    if decision_id is None:
        return False
    d = tables.decisions
    state = (await conn.execute(select(d.c.state).where(d.c.id == decision_id))).scalar_one_or_none()
    return state is not None and state != "open"


# Setting them


async def set_preferences(conn: AsyncConnection, user_id: int, values: dict) -> None:
    """Set member ``user_id``'s notification preferences named in ``values`` (PREFERENCES and hide_details), leaving
    the rest as they are; ``tiers`` is merged into the tiers they chose, a kind set to None going back to the default."""
    p = tables.notification_preferences
    values = dict(values)
    if "tiers" in values:
        tiers = chosen_tiers(await preferences(conn, user_id))
        for kind, tier in values["tiers"].items():
            if tier is None:
                tiers.pop(kind, None)
            else:
                tiers[kind] = tier
        values["tiers"] = tiers
    if not values:
        return
    chosen = pg_insert(p).values(user_id=user_id, **values)
    chosen = chosen.on_conflict_do_update(
        constraint="notification_preferences_pkey", set_={**values, "updated_at": func.now()}
    )
    await conn.execute(chosen)
