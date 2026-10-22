"""What the web's autonomy pages read (plan autonomy-levels, step 7), under /v1/autonomy:

- GET /v1/autonomy/settings: the caller's own autonomy for the page /settings/autonomy: the effective level and the
  mode of each class with its source, their preference, the ceiling the other sources set over it (the options above it
  show dimmed with its source), the stop that holds them, and for each class the hub records the figures of the last
  30 days: the acts refused and held for an answer (the ledger ``autonomy.decided``), the decisions asked in its
  categories, and for K5 and K6 the merges of their runs, the reverted ones and those PROD was rolled back from.
- GET /v1/autonomy/org: every seat's effective mode of each class, as its ceiling, its units' and the instance's set
  them, and the stops that hold; the org chart is everyone's to read, so is this.

The preview of a stop is GET /v1/autonomy/stops/preview (``autonomy_stops``). A run's agent reads none of these.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import autonomy_card, autonomy_levels, org, post_merge, tables
from evo_agents.hub.server import audit, autonomy_hold, autonomy_ledger
from evo_agents.hub.server.autonomy_page_models import (
    FIGURE_DAYS,
    AutonomySettings,
    ClassFigures,
    HeldStop,
    OrgAutonomy,
    SeatClass,
    SeatClasses,
    SettingsClass,
)
from evo_agents.hub.server.autonomy_store import newest_pref
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.org_store import load, today
from evo_agents.hub.server.security import CurrentUser, Principal

router = APIRouter(prefix="/v1/autonomy", tags=["autonomy"], responses={401: {"model": ErrorBody}})

RECORDED = tuple(f"K{n}" for n in range(5, 13))  # the classes a gate of the hub or the worker's hook records
DECIDED = re.compile(r" (K\d+) mode=\S+ result=(\w+)")
CATEGORY_CLASS = {category: klass for klass, held in autonomy_card.CLASS_CATEGORIES.items() for category in held}


def _agent_refusal(user: Principal) -> None:
    if user.scope is not None:
        raise HTTPException(403, "the agent of a run reads no autonomy page")


def held_stop(row) -> HeldStop:
    return HeldStop(
        id=row.id,
        scope=row.scope,
        target=autonomy_hold.scope_name(row.scope, row.login, row.slug),
        login=row.login,
        unit=row.slug,
        reason=row.reason,
        stopped_at=row.stopped_at,
    )


async def _ledger(conn: AsyncConnection, user_id: int, since) -> tuple[Counter, Counter]:
    """(refused, asked) of each class in the decisions of the ledger whose principal is ``user_id``."""
    a = tables.audit
    query = select(a.c.target).where(a.c.action == audit.AUTONOMY_DECIDED, a.c.actor_id == user_id, a.c.at >= since)
    refused, asked = Counter(), Counter()
    for (target,) in await conn.execute(query):
        found = DECIDED.search(target)
        if found is None:
            continue
        klass, result = found.groups()
        if result == autonomy_ledger.REFUSED:
            refused[klass] += 1
        elif result == autonomy_ledger.ASKED:
            asked[klass] += 1
    return refused, asked


async def _decisions(conn: AsyncConnection, user_id: int, since) -> Counter:
    """The decisions asked by the runs of ``user_id``, by the class of their category."""
    d, r = tables.decisions, tables.runs
    query = (
        select(d.c.category, func.count())
        .join_from(d, r, r.c.id == d.c.run_id)
        .where(r.c.principal_id == user_id, d.c.asked_at >= since, d.c.category.in_(list(CATEGORY_CLASS)))
        .group_by(d.c.category)
    )
    found = Counter()
    for category, count in await conn.execute(query):
        found[CATEGORY_CLASS[category]] += count
    return found


async def _merges(conn: AsyncConnection, user_id: int, since) -> tuple[int, int, int]:
    """(merged, reverted, rolled back) of the merges of the runs of ``user_id``."""
    q, r, e = tables.merge_queue, tables.runs, tables.merge_events
    mine = (
        select(q.c.id, q.c.state)
        .join_from(q, r, r.c.id == q.c.run_id)
        .where(r.c.principal_id == user_id, q.c.merged_at >= since, q.c.source != post_merge.REVERT)
    )
    rows = (await conn.execute(mine)).all()
    if not rows:
        return 0, 0, 0
    reverted = sum(1 for row in rows if row.state in post_merge.CULPRIT_STATES)
    rolled = select(func.count(func.distinct(e.c.queue_id))).where(
        e.c.queue_id.in_([row.id for row in rows]), e.c.kind == "rollback"
    )
    return len(rows), reverted, (await conn.execute(rolled)).scalar_one()


async def figures(conn: AsyncConnection, user_id: int) -> dict[str, ClassFigures]:
    """The figures of each class the hub records over the last FIGURE_DAYS days, for the runs of ``user_id``."""
    since = func.now() - timedelta(days=FIGURE_DAYS)
    refused, asked = await _ledger(conn, user_id, since)
    decisions = await _decisions(conn, user_id, since)
    merged, reverted, rolled_back = await _merges(conn, user_id, since)
    found = {
        klass: ClassFigures(refused=refused[klass], asked=asked[klass], decisions=decisions[klass])
        for klass in RECORDED
    }
    found["K5"] = found["K5"].model_copy(update={"merged": merged, "reverted": reverted})
    found["K6"] = found["K6"].model_copy(update={"rolled_back": rolled_back})
    return found


def _modes(klass: str) -> list[str]:
    return [mode for mode in autonomy_levels.MODES if mode != "ask" or klass not in autonomy_levels.NO_ASK]


def _classes(reached, ceiling, pref, counted: dict[str, ClassFigures]) -> dict[str, SettingsClass]:
    held = None if pref is None else autonomy_levels.cells(pref.level, pref.overrides)
    rank = autonomy_levels.mode_rank
    found = {}
    for klass in autonomy_levels.CLASSES:
        choice, top = reached.classes[klass], ceiling.classes[klass]
        wanted = None if held is None else held[klass].mode
        found[klass] = SettingsClass(
            name=autonomy_levels.CLASS_NAMES[klass],
            mode=choice.mode,
            source=choice.source,
            detail=choice.detail,
            modes=_modes(klass),
            ceiling=top.mode,
            ceiling_source=top.source,
            pref=wanted,
            lowered=wanted is not None and rank(choice.mode) < rank(wanted),
            figures=counted.get(klass),
        )
    return found


async def settings(conn: AsyncConnection, user: Principal) -> AutonomySettings:
    """The caller's own settings page, as the module says."""
    _agent_refusal(user)
    chart, day = await load(conn), await today(conn)
    pref = await newest_pref(conn, user.user_id)
    own = autonomy_levels.pref_source(user.login, *((None,) if pref is None else (pref.level, pref.overrides)))
    sources = org.autonomy_sources(chart, user.user_id, day, own)
    others = sources[1:]
    reached, ceiling = autonomy_levels.effective(sources), autonomy_levels.effective(others)
    detail = next((source.detail for source in others if source.name == ceiling.level_source), "")
    stop = await autonomy_hold.held_by(conn, user.user_id)
    return AutonomySettings(
        principal=user.login,
        level=reached.level,
        level_source=reached.level_source,
        revision=None if pref is None else pref.revision,
        pref_level=None if pref is None else pref.level,
        pref_classes={} if pref is None else dict(pref.overrides),
        ceiling=ceiling.level,
        ceiling_source=ceiling.level_source,
        ceiling_detail=detail,
        classes=_classes(reached, ceiling, pref, await figures(conn, user.user_id)),
        stop=None if stop is None else held_stop(stop),
    )


async def org_autonomy(conn: AsyncConnection, user: Principal) -> OrgAutonomy:
    """Every seat's effective mode of each class, and the stops that hold."""
    _agent_refusal(user)
    chart, day = await load(conn), await today(conn)
    seats = []
    for seat in sorted(chart.seats.values(), key=lambda seat: seat.id):
        reached = autonomy_levels.effective([*org.seat_sources(chart, seat, day), autonomy_levels.instance_source()])
        classes = {
            klass: SeatClass(mode=choice.mode, source=choice.source) for klass, choice in reached.classes.items()
        }
        seats.append(SeatClasses(id=seat.id, classes=classes))
    stops = [held_stop(row) for row in await conn.execute(autonomy_hold.live_stops())]
    return OrgAutonomy(seats=seats, stops=stops)


@router.get("/settings", response_model=AutonomySettings, responses={403: {"model": ErrorBody}})
async def show_settings(request: Request, user: CurrentUser) -> AutonomySettings:
    """The caller's own autonomy, preference, ceiling, stop and 30-day figures, for the web's settings page."""
    async with request.app.state.engine.connect() as conn:
        return await settings(conn, user)


@router.get("/org", response_model=OrgAutonomy, responses={403: {"model": ErrorBody}})
async def show_org_autonomy(request: Request, user: CurrentUser) -> OrgAutonomy:
    """Every seat's effective mode of each class, and the stops that hold, for the web's org page."""
    async with request.app.state.engine.connect() as conn:
        return await org_autonomy(conn, user)
