"""The job ``baseline`` of the plugin canvas (criterion a6 of plan living-lean-canvas): each canvas's weekly baseline,
proposed when its time comes.

When. The config's ``baseline`` names a day of the week and a time of day (16:00 on Friday by default) in a time zone:
``baseline.timezone``, else the zone of the project's Curator charter (its window's), else UTC; a zone Postgres does
not know is logged and the next in that order taken. The job runs every 15 minutes in UTC for every project that
enables canvas (``hub.plugin_jobs``). Once this week's time has passed in the zone, each canvas not archived that has
no baseline of this ISO week (the week of the day it was due) gets one, in a transaction of its own under the canvas's
lock; the week is the key, so a canvas gets one a week however often the job runs. A proposal of an earlier week, or
one a writer opened, that is still waiting expires first: its undecided claims are asked again.

What. The items of ``baselines.items_for``, today being the UTC day of Postgres's clock. A baseline whose items wait
for no member (new claims only, or none) is approved at once; any other sends the notice canvas_baseline to the
writers who may decide it (``notices``).
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from sqlalchemy import Text, column, func, literal, select, table
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.core_plugins.canvas import (
    BASELINE_CRON,
    BASELINE_DEFAULT,
    BASELINE_JOB,
    WEEKDAYS,
    baselines,
    notices,
    store,
)
from evo_agents.core_plugins.canvas.content import CanvasError
from evo_agents.core_plugins.canvas.store import CanvasRow
from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import CoreContext

log = logging.getLogger(__name__)

JOB = BASELINE_JOB
CRON = BASELINE_CRON
UTC_ZONE = "UTC"
PG_TIMEZONES = table("pg_timezone_names", column("name", Text))  # evo_agents.hub.server.curator.PG_TIMEZONES


@dataclass(frozen=True)
class Schedule:
    """When a project's canvases get their weekly baseline: a weekday (0 for Monday), a time of day, a time zone, and
    where the zone came from (config, charter or utc)."""

    weekday: int
    at: time
    zone: str
    zone_from: str


@dataclass(frozen=True)
class Due:
    """This week's baseline, its time come: the ISO week, and when it was due (in UTC)."""

    week: str
    due_at: datetime


async def configured(conn: AsyncConnection, project_id: int) -> dict:
    """The config's ``baseline`` in project ``project_id``, as its layers set it; {} when canvas is not enabled."""
    found = await baselines.config_value(conn, project_id, "baseline")
    return dict(found) if isinstance(found, dict) else {}


async def _known(conn: AsyncConnection, zone) -> bool:
    if not isinstance(zone, str) or not zone:
        return False
    return await conn.scalar(select(PG_TIMEZONES.c.name).where(PG_TIMEZONES.c.name == zone).limit(1)) is not None


async def _charter_zone(conn: AsyncConnection, project_id: int):
    c = tables.charters
    body = await conn.scalar(
        select(c.c.body).where(c.c.project_id == project_id).order_by(c.c.revision.desc()).limit(1)
    )
    return ((body or {}).get("window") or {}).get("timezone")


async def schedule_of(conn: AsyncConnection, project_id: int) -> Schedule:
    """The schedule of the weekly baselines of project ``project_id``."""
    found = {**BASELINE_DEFAULT, **(await configured(conn, project_id))}
    hours, minutes = (int(part) for part in str(found["time"]).split(":"))
    weekday = WEEKDAYS.index(found["weekday"]) if found["weekday"] in WEEKDAYS else WEEKDAYS.index("friday")
    for zone, source in ((found.get("timezone"), "config"), (await _charter_zone(conn, project_id), "charter")):
        if await _known(conn, zone):
            return Schedule(weekday, time(hours, minutes), zone, source)
        if zone:
            log.warning("a baseline's time zone is unknown", extra={"project_id": project_id, "zone": zone})
    return Schedule(weekday, time(hours, minutes), UTC_ZONE, "utc")


def week_of(local_now: datetime, schedule: Schedule) -> tuple[str, datetime] | None:
    """(the ISO week, the local moment it was due) of this week's baseline once ``local_now`` (a time in the
    schedule's zone, without one) reached it; None before."""
    monday = local_now.date() - timedelta(days=local_now.weekday())
    day = monday + timedelta(days=schedule.weekday)
    moment = datetime.combine(day, schedule.at)
    if local_now < moment:
        return None
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}", moment


async def due(conn: AsyncConnection, schedule: Schedule) -> Due | None:
    """This week's baseline in ``schedule`` when its time has come, by Postgres's clock; None before."""
    local_now = await conn.scalar(select(func.timezone(schedule.zone, func.now())))
    found = week_of(local_now, schedule)
    if found is None:
        return None
    week, moment = found
    return Due(week, await conn.scalar(select(func.timezone(schedule.zone, literal(moment)))))


async def _has_week(conn: AsyncConnection, canvas: CanvasRow, week: str) -> bool:
    b = tables.canvas.baselines
    return await conn.scalar(select(b.c.id).where(b.c.canvas_id == canvas.id, b.c.week == week)) is not None


async def propose_week(conn: AsyncConnection, project_id: int, canvas: CanvasRow, when: Due, zone: str) -> Counter:
    """Propose this week's baseline of ``canvas``, which the caller locked, unless it has one; what it did."""
    done = Counter()
    if await _has_week(conn, canvas, when.week):
        return done
    done["expired"] += await baselines.expire_open(conn, canvas)
    found = await baselines.items_for(conn, canvas, await baselines.today(conn))
    made = await baselines.propose(conn, project_id, canvas, found, week=when.week, due_at=when.due_at, time_zone=zone)
    done["baselines"] += 1
    done[made.status] += 1  # proposed, or approved at once when no item waits for a member
    if made.status == "proposed":
        told = await notices.tell_writers(conn, project_id, canvas, made, await baselines.items(conn, made.id))
        done["told"] += len(told)
    return done


async def run_project(engine: AsyncEngine, project: str) -> dict:
    """One pass of the job over the canvases of ``project``; what it did, counted."""
    async with engine.begin() as conn:
        project_id = await conn.scalar(select(tables.projects.c.id).where(tables.projects.c.name == project))
        if project_id is None:
            return {}
        schedule = await schedule_of(conn, project_id)
        when = await due(conn, schedule)
        canvases = await store.project_canvases(conn, project_id) if when is not None else []
    done = Counter()
    for found in canvases:
        try:
            async with engine.begin() as conn:
                canvas = await store.lock_canvas(conn, found.id)
                if canvas is not None:
                    done += await propose_week(conn, project_id, canvas, when, schedule.zone)
        except CanvasError as exc:
            log.warning(
                "a canvas's baseline failed", extra={"project": project, "canvas": found.slug, "why": exc.message}
            )
    done = +done
    if done:
        log.info("baselines", extra={"project": project, "week": when.week, **done})
    return dict(done)


async def run(params: dict, context: CoreContext) -> dict:
    """The job ``baseline``: one pass over each project ``params`` names (``projects``)."""
    return {
        "projects": {project: await run_project(context.engine, project) for project in params.get("projects") or []}
    }


__all__ = ["CRON", "JOB", "Due", "Schedule", "due", "propose_week", "run", "run_project", "schedule_of", "week_of"]
