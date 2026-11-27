"""The tool figures of runs (``evo_agents.hub.server.tool_stats``): how the tool calls of a run add up, writing them
when it ends (``record``), and reading them for one run or summed over a project's runs that ended lately."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime, time, timedelta

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.digest import MAX_NAME, SKILL_TOOL
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.runs.models.tool_stats import (
    ENDED,
    NAMED_BY_TITLE,
    UNNAMED,
    ProjectToolStats,
    RuntimeSkillStat,
    RuntimeToolStat,
    RunToolStats,
    SkillStat,
    ToolFigures,
    ToolStat,
)
from evo_agents.hub.server.runs.repository import tool_stats as repository
from evo_agents.hub.server.runs.service.views import readable_run, visible_plans


def tool_name(runtime: str, title: str | None, kind: str | None) -> str:
    """The name the figures of a call go under: see ``evo_agents.hub.server.tool_stats``."""
    title, kind = (title or "").strip(), (kind or "").strip()
    if runtime == NAMED_BY_TITLE and title:
        name = title
    elif kind and kind != "other":
        name = kind
    else:
        name = title or kind or UNNAMED
    return name[:MAX_NAME]


def _skills(loaded: dict[str, str], failed: set[str]) -> list[dict]:
    """Per skill of ``loaded`` (call id: skill), its calls and the calls that failed, the most called first."""
    calls, errors = Counter(loaded.values()), Counter(name for call, name in loaded.items() if call in failed)
    ranked = sorted(calls.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"skill": name, "calls": n, "errors": errors[name]} for name, n in ranked]


def summarize(runtime: str, events: Iterable) -> list[ToolFigures]:
    """The figures per tool of a run of ``runtime`` whose tool events, in seq order, are ``events``: each with ``at``,
    ``kind``, ``call_id``, ``title``, ``tool_kind``, ``status`` and, for a call of the Skill tool, ``skill``. The most
    called first."""
    started: dict[str, tuple[str, datetime]] = {}  # call id: (tool, when it started)
    ended: dict[str, datetime] = {}
    failed: set[str] = set()
    loaded: dict[str, str] = {}  # call id of the Skill tool: the skill
    for event in events:
        call = event.call_id
        if not call:
            continue
        if event.kind == "tool_call":
            if call in started:
                continue  # a call is counted once
            started[call] = (tool_name(runtime, event.title, event.tool_kind), event.at)
            skill = getattr(event, "skill", None)
            if started[call][0] == SKILL_TOOL and runtime == NAMED_BY_TITLE and isinstance(skill, str) and skill:
                loaded[call] = skill[:MAX_NAME]
        elif call not in started:
            continue  # an update of a call whose start is not in the log
        if event.status == "failed":
            failed.add(call)
        if event.status in ENDED and call not in ended:
            ended[call] = event.at
    figures: dict[str, ToolFigures] = {}
    for call, (name, at) in started.items():
        found = figures.setdefault(name, ToolFigures(name))
        found.calls += 1
        found.errors += call in failed
        if call in ended:
            found.duration_ms += max(0, round((ended[call] - at).total_seconds() * 1000))
    if loaded:
        figures[SKILL_TOOL].skills = _skills(loaded, failed)
    return sorted(figures.values(), key=lambda item: (-item.calls, item.name))


async def record(conn: AsyncConnection, run_id: int) -> list[ToolFigures]:
    """Write the figures of run ``run_id`` from its events, in place of the ones it had; what was written."""
    runtime = await repository.runtime_of(conn, run_id)
    figures = summarize(runtime, await repository.tool_events(conn, run_id))
    rows = [
        {
            "run_id": run_id,
            "tool_name": item.name,
            "calls": item.calls,
            "errors": item.errors,
            "duration_ms": item.duration_ms,
            "skills": item.skills or None,
        }
        for item in figures
    ]
    await repository.replace(conn, run_id, rows)
    return figures


def skill_runs(rows) -> list[RuntimeSkillStat]:
    """Per skill, the calls, failed calls and runs of ``rows`` (each with a run's ``skills``), the most called first."""
    found: dict[str, Counter] = {}
    for row in rows:
        for item in row.skills or []:
            counted = found.setdefault(item["skill"], Counter())
            counted.update(calls=item["calls"], errors=item["errors"], runs=1)
    ranked = sorted(found.items(), key=lambda kv: (-kv[1]["calls"], kv[0]))
    return [RuntimeSkillStat(skill=name, **counted) for name, counted in ranked]


async def run_tool_stats(engine: AsyncEngine, user, project: str, run_id: int, sink: str | None) -> RunToolStats:
    """What the tool calls of run ``run_id`` came to, per tool, for whoever may read the run."""
    async with UnitOfWork(engine) as conn:
        await readable_run(conn, user, project, run_id, sink)
        run = await repository.run_row(conn, run_id)
        rows = await repository.run_stats(conn, run_id)
    tools = [
        ToolStat(  # pyright: ignore[reportCallIssue] (populate_by_name)
            name=row.tool_name,  # pyright: ignore[reportCallIssue]
            calls=row.calls,
            errors=row.errors,
            duration_ms=row.duration_ms,
        )
        for row in rows
    ]
    skills = [SkillStat(**item) for row in rows for item in row.skills or []]
    return RunToolStats(
        run_id=run_id, runtime=run.runtime, state=run.state, finished_at=run.finished_at, tools=tools, skills=skills
    )


async def project_tool_stats(
    engine: AsyncEngine, user, project: str, days: int, plan_id: str | None, runtime: str | None, sink: str | None
) -> ProjectToolStats:
    """The tool figures of the runs of the plans the caller may read that ended in the last ``days`` days in UTC,
    today included, per tool and runtime."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        plans = await visible_plans(conn, access, sink)
        today = await repository.utc_today(conn)
        first = today - timedelta(days=days - 1)
        since = datetime.combine(first, time.min, tzinfo=UTC)
        until = datetime.combine(today + timedelta(days=1), time.min, tzinfo=UTC)
        ended = repository.ended_runs(access.project_id, plans, since, until, plan_id, runtime)
        counted = await repository.count_runs(conn, ended)
        rows = await repository.per_tool(conn, ended)
        skills = skill_runs(await repository.skill_loads(conn, ended))
    tools = [
        RuntimeToolStat(  # pyright: ignore[reportCallIssue] (populate_by_name)
            name=row.tool_name,  # pyright: ignore[reportCallIssue]
            runtime=row.runtime,
            calls=int(row.calls),
            errors=int(row.errors),
            duration_ms=int(row.duration_ms),
            runs=row.runs,
        )
        for row in rows
    ]
    return ProjectToolStats(
        project=access.name, days=days, first_day=first, last_day=today, runs=counted, tools=tools, skills=skills
    )
