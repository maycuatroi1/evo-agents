"""What the hub tells the owner of an open opportunity as its window closes (plan opportunity-intake, criterion a4),
without the plugin pm.

``remind`` is the job ``hub.opportunity_deadlines``, every hour in the hub's worker. For each opportunity whose newest
revision is open and has a ``window_until``, on the UTC date of the pass, the owner gets one notice
(``opportunity_window``, on the web and every channel of theirs) at the first milestone it reached:

- ``due_14``: 14 days or fewer are left, more than 3;
- ``due_3``: 3 days or fewer are left, the date itself included;
- ``past``: the date passed and the opportunity is still open; the notice asks to close it as captured or missed, or to
  move the window.

Each milestone is sent once per value of ``window_until``: the notice's details name the code, the date and the
milestone, and a pass looks them up before it sends, under the opportunity's row lock. A revision that moves the date
makes a new value, so the milestones count again; one that closes the opportunity ends the reminders. A pass that finds
an opportunity at a later milestone than the one it last told (the hub was down, or the window was set 5 days before
its end) sends the later one alone: an earlier milestone is never sent late. The hub never changes the status of an
opportunity: the reminder only asks the owner to.

``closing`` is what ``hub opportunity list --closing-within DAYS`` reads: the open opportunities whose window ends
within DAYS days of today, one already past included, the nearest end first.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, timedelta

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import opportunities, tables
from evo_agents.hub.server.notifications import notify

log = logging.getLogger(__name__)

NOTICE = "opportunity_window"  # runs.NOTICE_KINDS
FAR, NEAR = 14, 3  # days left at the first and the second milestone
MILESTONES = ("due_14", "due_3", "past")


def today_utc():
    """The database's date in UTC, as an expression."""
    return cast(func.timezone("UTC", func.now()), Date)


def milestone(until: date, today: date) -> str | None:
    """The milestone a window ending on ``until`` has reached on ``today``, None while more than FAR days are left."""
    left = (until - today).days
    if left < 0:
        return "past"
    if left <= NEAR:
        return "due_3"
    if left <= FAR:
        return "due_14"
    return None


def _words(code: str, until: date, today: date, reached: str) -> tuple[str, str]:
    left = (until - today).days
    if reached == "past":
        title = f"Window of {code} closed on {until.isoformat()} and it is still open"
        ask = (
            f"Close {code} as captured or missed (`evo-agents hub opportunity close {code} --status captured|missed "
            "--reason TEXT`), or move the window (`evo-agents hub opportunity set "
            f"{code} --window-until DATE`). The hub changes nothing on its own."
        )
    else:
        days = f"{left} day{'' if left == 1 else 's'}"
        title = f"Window of {code} closes on {until.isoformat()}: {days} left"
        ask = (
            f"Capture {code} before then, or move the window (`evo-agents hub opportunity set {code} --window-until "
            "DATE`) if it moved. The hub changes nothing on its own."
        )
    return title, ask


async def _open_windows(conn: AsyncConnection, project_id: int | None = None):
    o, r = tables.opportunities, tables.opportunity_revisions
    query = (
        select(o.c.id, o.c.project_id, o.c.number, r.c.window_until, r.c.owner_id, r.c.statement)
        .join_from(o, r, (r.c.opportunity_id == o.c.id) & (r.c.revision == o.c.revision))
        .where(r.c.status == opportunities.OPEN, r.c.window_until.is_not(None))
        .order_by(o.c.project_id, o.c.number)
    )
    if project_id is not None:
        query = query.where(o.c.project_id == project_id)
    return list(await conn.execute(query))


async def _remind_one(conn: AsyncConnection, opportunity_id: int, project: str, today: date) -> str | None:
    """One opportunity, in a transaction of its own that holds its row; the milestone it sent, if any."""
    o, r, n = tables.opportunities, tables.opportunity_revisions, tables.notifications
    await conn.scalar(select(o.c.id).where(o.c.id == opportunity_id).with_for_update())
    row = (
        await conn.execute(
            select(o.c.project_id, o.c.number, r.c.window_until, r.c.owner_id, r.c.status)
            .join_from(o, r, (r.c.opportunity_id == o.c.id) & (r.c.revision == o.c.revision))
            .where(o.c.id == opportunity_id)
        )
    ).first()  # read again under the lock: a revision may have moved the window or closed it
    if row is None or row.status != opportunities.OPEN or row.window_until is None:
        return None
    reached = milestone(row.window_until, today)
    if reached is None:
        return None
    code = opportunities.code(row.number)
    until = row.window_until.isoformat()
    told = select(n.c.id).where(
        n.c.project_id == row.project_id,
        n.c.notice_kind == NOTICE,
        n.c.details["code"].astext == code,
        n.c.details["window_until"].astext == until,
        n.c.details["milestone"].astext == reached,
    )
    if await conn.scalar(told.limit(1)) is not None:
        return None
    title, ask = _words(code, row.window_until, today, reached)
    await notify(
        conn,
        user_id=row.owner_id,
        kind="notice",
        notice_kind=NOTICE,
        project_id=row.project_id,
        title=title,
        body=f"{title} (project {project}).\n\n{ask}",
        details={"code": code, "window_until": until, "milestone": reached},
    )
    return reached


async def remind(engine: AsyncEngine, *, today: date | None = None) -> dict:
    """One pass of ``hub.opportunity_deadlines`` on ``today`` (the UTC date of the database when None): how many
    notices went out for each milestone, and the opportunities that failed."""
    async with engine.begin() as conn:
        day = today or await conn.scalar(select(today_utc()))
        found = [row for row in await _open_windows(conn) if milestone(row.window_until, day) is not None]
        names = {
            pid: name
            for pid, name in await conn.execute(
                select(tables.projects.c.id, tables.projects.c.name).where(
                    tables.projects.c.id.in_({row.project_id for row in found})
                )
            )
        }
    sent: Counter = Counter()
    for row in found:
        try:
            async with engine.begin() as conn:
                reached = await _remind_one(conn, row.id, names.get(row.project_id, ""), day)
            if reached:
                sent[reached] += 1
        except Exception:  # one opportunity failing leaves the others their reminders
            log.exception("an opportunity reminder failed", extra={"opportunity": row.id})
            sent["failed"] += 1
    if sent:
        log.info("opportunity reminders", extra=dict(sent))
    return dict(sorted(sent.items()))


def closing_filter(query, days: int):
    """``query`` (an opportunity at its revision, ``server.opportunities._select``) narrowed to the open ones whose
    window ends within ``days`` days of today, ordered nearest end first."""
    r = tables.opportunity_revisions
    until = today_utc() + timedelta(days=days)
    return query.where(r.c.status == opportunities.OPEN, r.c.window_until.is_not(None), r.c.window_until <= until)


__all__ = ["FAR", "MILESTONES", "NEAR", "NOTICE", "closing_filter", "milestone", "remind"]
