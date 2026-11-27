"""The SQL of the KG optimize loop (``evo_agents.hub.server.kg_optimize``): the rows of kg_optimize_runs with their
project and night shift, and what a run reads of the searches, the builds, the review run and its proposals."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.kg.models.optimize import MAX_EXPLORED
from evo_agents.hub.tables_kg import OPTIMIZE_ACTIVE
from evo_agents.kg import explore

NIGHT_SHIFT = "night_shift"  # schedules.kind of a project's night shift (kg_health.NIGHT_SHIFT)


async def now(conn: AsyncConnection) -> datetime:
    return (await conn.execute(select(func.now()))).scalar_one()


# The searches of real sessions


async def search_counts(conn: AsyncConnection, project_id: int, since: datetime):
    """(searches, clicked, grepped) of kg_search since ``since`` (kg_query_labels)."""
    t = tables.kg_query_labels
    when = func.coalesce(t.c.searched_at, t.c.created_at)
    clicked = func.count().filter(func.jsonb_array_length(t.c.clicks) > 0)
    grepped = func.count().filter(t.c.grep_after)
    query = select(func.count().label("searches"), clicked.label("clicked"), grepped.label("grepped")).where(
        t.c.project_id == project_id, when >= since
    )
    return (await conn.execute(query)).one()


async def new_labels(conn: AsyncConnection, project_id: int, since: datetime) -> int | None:
    t = tables.kg_query_labels
    return await conn.scalar(select(func.count()).where(t.c.project_id == project_id, t.c.created_at >= since))


async def explored_searches(conn: AsyncConnection, project_id: int, since: datetime) -> Sequence[Any]:
    """(results, clicks, grep_after) of the searches since ``since`` that showed an explored result."""
    t = tables.kg_query_labels
    when = func.coalesce(t.c.searched_at, t.c.created_at)
    query = (
        select(t.c.results, t.c.clicks, t.c.grep_after)
        .where(
            t.c.project_id == project_id,
            when >= since,
            or_(*(t.c.results.contains([{"explored": r}]) for r in explore.RANKS)),
        )
        .limit(MAX_EXPLORED)
    )
    return (await conn.execute(query)).all()


# The builds


async def build_coverage(conn: AsyncConnection, build_id: int):
    b = tables.kg_builds
    return await conn.scalar(select(b.c.coverage).where(b.c.id == build_id))


async def newest_build_since(conn: AsyncConnection, project_id: int, queued_at: datetime):
    """(id, status) of the newest build of the project queued at ``queued_at`` or later, None without one."""
    b = tables.kg_builds
    query = (
        select(b.c.id, b.c.status)
        .where(b.c.project_id == project_id, b.c.queued_at >= queued_at)
        .order_by(b.c.id.desc())
        .limit(1)
    )
    return (await conn.execute(query)).first()


# The rows


def context():
    """The active runs with their project and its night shift's owner."""
    o, p, s, u = tables.kg_optimize_runs, tables.projects, tables.schedules, tables.users
    return (
        select(
            o,
            p.c.name.label("project"),
            s.c.id.label("schedule_id"),
            s.c.owner_id,
            u.c.login.label("owner"),
        )
        .join_from(o, p, p.c.id == o.c.project_id)
        .join(s, and_(s.c.project_id == o.c.project_id, s.c.kind == NIGHT_SHIFT), isouter=True)
        .join(u, u.c.id == s.c.owner_id, isouter=True)
    )


async def active_contexts(conn: AsyncConnection) -> Sequence[Any]:
    o = tables.kg_optimize_runs
    return (await conn.execute(context().where(o.c.state.in_(OPTIMIZE_ACTIVE)).order_by(o.c.id))).all()


async def set_run(conn: AsyncConnection, run_id: int, **values) -> None:
    o = tables.kg_optimize_runs
    await conn.execute(update(o).where(o.c.id == run_id).values(**values, updated_at=func.now()))


async def has_config(conn: AsyncConnection, project_id: int) -> bool:
    c = tables.kg_configs
    return (await conn.execute(select(c.c.project_id).where(c.c.project_id == project_id))).first() is not None


async def active_of(conn: AsyncConnection, project_id: int):
    o = tables.kg_optimize_runs
    query = select(o).where(o.c.project_id == project_id, o.c.state.in_(OPTIMIZE_ACTIVE))
    return (await conn.execute(query)).one_or_none()


async def of_agent_run(conn: AsyncConnection, run_id: int):
    """The KG optimize run whose review run ``run_id`` is, None when none is."""
    o = tables.kg_optimize_runs
    return (await conn.execute(select(o).where(o.c.agent_run_id == run_id).order_by(o.c.id.desc()).limit(1))).first()


async def night_row(conn: AsyncConnection, project_id: int, night: date):
    o = tables.kg_optimize_runs
    query = select(o).where(o.c.project_id == project_id, o.c.via == "night", o.c.night == night)
    return (await conn.execute(query)).one_or_none()


async def insert_run(conn: AsyncConnection, **values):
    """Insert a run of kg_optimize_runs; the row."""
    o = tables.kg_optimize_runs
    return (await conn.execute(insert(o).values(**values).returning(o))).one()


async def previous_created(conn: AsyncConnection, project_id: int, run_id: int) -> datetime | None:
    """When the project's run before ``run_id`` was created, None for its first."""
    o = tables.kg_optimize_runs
    return await conn.scalar(
        select(o.c.created_at).where(o.c.project_id == project_id, o.c.id < run_id).order_by(o.c.id.desc())
    )


async def runs_of(conn: AsyncConnection, project_id: int, limit: int) -> Sequence[Any]:
    o = tables.kg_optimize_runs
    return (
        await conn.execute(select(o).where(o.c.project_id == project_id).order_by(o.c.id.desc()).limit(limit))
    ).all()


async def ended_since(conn: AsyncConnection, project_id: int, floor: datetime):
    """(id, state, body, report) of the project's newest run that ended at ``floor`` or later, None without one."""
    o = tables.kg_optimize_runs
    query = (
        select(o.c.id, o.c.state, o.c.body, o.c.report)
        .where(o.c.project_id == project_id, o.c.ended_at.is_not(None), o.c.ended_at >= floor)
        .order_by(o.c.ended_at.desc())
        .limit(1)
    )
    return (await conn.execute(query)).first()


async def project_name(conn: AsyncConnection, project_id: int) -> str:
    query = select(tables.projects.c.name).where(tables.projects.c.id == project_id)
    return (await conn.execute(query)).scalar_one()


# The night shift and the review run


async def night_shift_id(conn: AsyncConnection, project_id: int) -> int | None:
    s = tables.schedules
    return await conn.scalar(select(s.c.id).where(s.c.project_id == project_id, s.c.kind == NIGHT_SHIFT))


async def due_schedule(conn: AsyncConnection, project_id: int):
    """The due row of the project's schedule (``curator.repository.shift.due_schedules``), None without one."""
    from evo_agents.hub.server.curator.repository.shift import due_schedules

    query = due_schedules(None).where(tables.schedules.c.project_id == project_id)
    return (await conn.execute(query)).one_or_none()


async def night_review_run(conn: AsyncConnection, schedule_id: int, night: date) -> int | None:
    """The review run of the night shift ``schedule_id`` on ``night`` (curator_figures), None before it is queued."""
    cf = tables.curator_figures
    return await conn.scalar(select(cf.c.run_id).where(cf.c.schedule_id == schedule_id, cf.c.night == night))


async def run_state(conn: AsyncConnection, run_id: int) -> str | None:
    r = tables.runs
    return await conn.scalar(select(r.c.state).where(r.c.id == run_id))


async def pulls(conn: AsyncConnection, agent_run_id: int) -> Sequence[Any]:
    """The changes of the proposals of run ``agent_run_id``."""
    p, c = tables.proposals, tables.curator_changes
    query = (
        select(c.c.id, c.c.proposal_id, c.c.repo, c.c.branch, c.c.pr_url, c.c.state, c.c.merge_sha, c.c.merged_at)
        .join_from(c, p, p.c.id == c.c.proposal_id)
        .where(p.c.run_id == agent_run_id)
        .order_by(c.c.id)
    )
    return (await conn.execute(query)).all()
