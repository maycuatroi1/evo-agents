"""The SQL of the tool figures of runs (``evo_agents.hub.server.tool_stats``): a run's tool events, its rows of
run_tool_stats, and their sums over the runs that ended in a span of days."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, cast, delete, distinct, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server.runs.models.tool_stats import TOOL_EVENTS


async def runtime_of(conn: AsyncConnection, run_id: int) -> str:
    r = tables.runs
    return (await conn.execute(select(r.c.runtime).where(r.c.id == run_id))).scalar_one()


async def tool_events(conn: AsyncConnection, run_id: int) -> list:
    """The tool events of run ``run_id`` in seq order: ``at``, ``kind``, ``call_id``, ``title``, ``tool_kind``,
    ``status`` and ``skill``."""
    e = tables.run_events
    body = e.c.body
    query = (
        select(
            e.c.at,
            e.c.kind,
            body["toolCallId"].astext.label("call_id"),
            body["title"].astext.label("title"),
            body["kind"].astext.label("tool_kind"),
            body["status"].astext.label("status"),
            body["rawInput"]["skill"].astext.label("skill"),
        )
        .where(e.c.run_id == run_id, e.c.kind.in_(TOOL_EVENTS))
        .order_by(e.c.seq)
    )
    return list((await conn.execute(query)).all())


async def replace(conn: AsyncConnection, run_id: int, rows: list[dict]) -> None:
    """Put ``rows`` in place of the rows of run_tool_stats of run ``run_id``."""
    s = tables.run_tool_stats
    await conn.execute(delete(s).where(s.c.run_id == run_id))
    if rows:
        await conn.execute(insert(s), rows)


async def run_row(conn: AsyncConnection, run_id: int):
    r = tables.runs
    return (await conn.execute(select(r.c.runtime, r.c.state, r.c.finished_at).where(r.c.id == run_id))).one()


async def run_stats(conn: AsyncConnection, run_id: int) -> list:
    """The rows of run_tool_stats of run ``run_id``, the most called first."""
    s = tables.run_tool_stats
    query = (
        select(s.c.tool_name, s.c.calls, s.c.errors, s.c.duration_ms, s.c.skills)
        .where(s.c.run_id == run_id)
        .order_by(s.c.calls.desc(), s.c.tool_name)
    )
    return list((await conn.execute(query)).all())


async def utc_today(conn: AsyncConnection) -> date:
    return (await conn.execute(select(cast(func.timezone("UTC", func.now()), Date)))).scalar_one()


def ended_runs(
    project_id: int, plans, since: datetime, until: datetime, plan_id: str | None, runtime: str | None
) -> list:
    """The conditions on runs of the project of ``plans`` that ended from ``since`` to before ``until``, of
    ``plan_id`` and ``runtime`` when given."""
    r = tables.runs
    ended = [
        r.c.project_id == project_id,
        r.c.plan_id.in_(plans),
        r.c.state.in_(runs.TERMINAL_STATES),
        r.c.finished_at >= since,
        r.c.finished_at < until,
    ]
    if plan_id is not None:
        ended.append(r.c.plan_id == plan_id)
    if runtime is not None:
        ended.append(r.c.runtime == runtime)
    return ended


async def count_runs(conn: AsyncConnection, ended: list) -> int:
    r = tables.runs
    return (await conn.execute(select(func.count()).select_from(r).where(*ended))).scalar_one()


async def per_tool(conn: AsyncConnection, ended: list) -> list:
    """The sums of the figures of the runs ``ended`` picks per tool and runtime, the most called first."""
    r, s = tables.runs, tables.run_tool_stats
    calls = func.sum(s.c.calls)
    query = (
        select(
            s.c.tool_name,
            r.c.runtime,
            calls.label("calls"),
            func.sum(s.c.errors).label("errors"),
            func.sum(s.c.duration_ms).label("duration_ms"),
            func.count(distinct(s.c.run_id)).label("runs"),
        )
        .join_from(s, r, r.c.id == s.c.run_id)
        .where(*ended)
        .group_by(s.c.tool_name, r.c.runtime)
        .order_by(calls.desc(), s.c.tool_name, r.c.runtime)
    )
    return list((await conn.execute(query)).all())


async def skill_loads(conn: AsyncConnection, ended: list) -> list:
    """The ``skills`` of the rows of the runs ``ended`` picks that loaded a skill."""
    r, s = tables.runs, tables.run_tool_stats
    loads = select(s.c.skills).join_from(s, r, r.c.id == s.c.run_id).where(*ended, s.c.skills.is_not(None))
    return list((await conn.execute(loads)).all())
