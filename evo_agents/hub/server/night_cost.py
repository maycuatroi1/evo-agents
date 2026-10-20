"""What a night of a project's night shift spent: how many runs its schedule queued, what they cost, and the run of the
schedule queued or held now (``evo_agents.hub.server.curator``, which imports them back). A night's cost is that of its
runs (their ``usage``), each agent session once at its largest total, as Claude Code reports a session's running
total; ``kind_cost`` counts the runs of one kind alone, as the night shift caps its replays (``replays``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import Numeric, Text, case, cast, func, literal, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables

QUEUED_OR_HELD = ("queued", *runs.HELD_STATES)  # a schedule queues its next run once none of its runs is in these


def usage_cost(usage):
    """A run's cost from its usage, as ``curator.run_cost`` reads it: total_cost_usd, else cost, else 0."""
    total, other = usage["total_cost_usd"], usage["cost"]
    return case(
        (func.jsonb_typeof(total) == "number", func.greatest(cast(total, Numeric), 0)),
        (func.jsonb_typeof(other) == "number", func.greatest(cast(other, Numeric), 0)),
        else_=0,
    )


@dataclass(frozen=True)
class NightFigures:
    runs: int
    cost_usd: float
    active_run_id: int | None


def _night_figures(schedule_id: int, night: date):
    """One row: how many runs schedule ``schedule_id`` queued for ``night``, what they cost (each session once, at
    its largest total, a run without a session on its own), and the run of the schedule queued or held now."""
    r = tables.runs
    of_night = (r.c.schedule_id == schedule_id, r.c.schedule_night == night)
    session = func.coalesce(r.c.session_id, literal("run:") + cast(r.c.id, Text))
    per_session = select(func.max(usage_cost(r.c.usage)).label("cost")).where(*of_night).group_by(session).subquery()
    count = select(func.count()).select_from(r).where(*of_night).scalar_subquery()
    cost = select(func.coalesce(func.sum(per_session.c.cost), 0)).scalar_subquery()
    active = (
        select(r.c.id)
        .where(r.c.schedule_id == schedule_id, r.c.state.in_(QUEUED_OR_HELD))
        .order_by(r.c.id)
        .limit(1)
        .scalar_subquery()
    )
    return select(count.label("runs"), cost.label("cost"), active.label("active_run_id"))


async def night_figures(conn: AsyncConnection, schedule_id: int, night: date) -> NightFigures:
    row = (await conn.execute(_night_figures(schedule_id, night))).one()
    return NightFigures(runs=row.runs, cost_usd=float(row.cost or 0), active_run_id=row.active_run_id)


async def kind_cost(conn: AsyncConnection, schedule_id: int, night: date, kind: str) -> float:
    """What the runs of ``kind`` schedule ``schedule_id`` queued for ``night`` cost, each session once."""
    r = tables.runs
    of_night = (r.c.schedule_id == schedule_id, r.c.schedule_night == night, r.c.kind == kind)
    session = func.coalesce(r.c.session_id, literal("run:") + cast(r.c.id, Text))
    per_session = select(func.max(usage_cost(r.c.usage)).label("cost")).where(*of_night).group_by(session).subquery()
    cost = (await conn.execute(select(func.coalesce(func.sum(per_session.c.cost), 0)))).scalar_one()
    return float(cost or 0)
