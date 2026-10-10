"""What the tool calls of a run came to: per tool, the calls, the calls that failed and the time they took. The hub
writes them when the run ends, so they outlive the run's events, which hub.prune_run_events deletes after
EVO_HUB_RUN_LOG_DAYS.

``record`` reads the run's tool_call and tool_call_update events (``docs/workers.md``, "Event kinds") and replaces the
run's rows of run_tool_stats with what ``summarize`` makes of them. ``run_state.move_run`` calls it in the transaction
of every move to an end state; ``run_events.post_events`` calls it again for events that arrive after the end, since a
worker's spool may send its last ones then; and ``run_state.prune_run_events`` calls it just before it deletes a run's
events, so a run that ended before schema 0013 keeps its figures too.

A call is a tool_call event, matched to its updates by ``toolCallId``. It failed when an update says ``failed``, and
it took the time from its tool_call to its first update that says ``completed`` or ``failed``, by the times the worker
gave the events; a call that never finished counts no time. The tool's name, ``gen_ai.tool.name`` as OpenTelemetry's
GenAI conventions call it, is the title Claude Code gives a call, which is its tool's name. Codex and opencode title a
call by what it does (the command, the file), so for them the name is the call's kind (execute, edit, read, search,
fetch, think), and the title for a call of kind other, which is an MCP tool (``server/tool`` for Codex).

GET /v1/projects/{project}/runs/{run_id}/tool-stats shows one run's figures to whoever may read the run
(``runs.readable_run``). GET /v1/projects/{project}/tool-stats adds them up per tool and runtime over the runs of the
plans the caller may read that ended on the last ``days`` days in UTC, today included, filtered by plan and runtime.
The MCP tool run_tool_stats answers both.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Date, cast, delete, distinct, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.digest import MAX_NAME, TOOL_NAME
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.runs.service.views import readable_run, visible_plans
from evo_agents.hub.server.security import CurrentUser

TOOL_EVENTS = ("tool_call", "tool_call_update")
ENDED = ("completed", "failed")  # the statuses that end a call
NAMED_BY_TITLE = "claude-code"  # the runtime whose title of a call is its tool's name
UNNAMED = "unknown"
STATS_DAYS = 7
MAX_STATS_DAYS = 90
READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}

router = APIRouter(prefix="/v1/projects", tags=["runs"], responses={401: {"model": ErrorBody}})


# Figures


def tool_name(runtime: str, title: str | None, kind: str | None) -> str:
    """The name the figures of a call go under: see the module."""
    title, kind = (title or "").strip(), (kind or "").strip()
    if runtime == NAMED_BY_TITLE and title:
        name = title
    elif kind and kind != "other":
        name = kind
    else:
        name = title or kind or UNNAMED
    return name[:MAX_NAME]


@dataclass
class ToolFigures:
    name: str
    calls: int = 0
    errors: int = 0
    duration_ms: int = 0


def summarize(runtime: str, events: Iterable) -> list[ToolFigures]:
    """The figures per tool of a run of ``runtime`` whose tool events, in seq order, are ``events``: each with ``at``,
    ``kind``, ``call_id``, ``title``, ``tool_kind`` and ``status``. The most called first."""
    started: dict[str, tuple[str, datetime]] = {}  # call id: (tool, when it started)
    ended: dict[str, datetime] = {}
    failed: set[str] = set()
    for event in events:
        call = event.call_id
        if not call:
            continue
        if event.kind == "tool_call":
            if call in started:
                continue  # a call is counted once
            started[call] = (tool_name(runtime, event.title, event.tool_kind), event.at)
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
    return sorted(figures.values(), key=lambda item: (-item.calls, item.name))


def _tool_events(run_id: int):
    e = tables.run_events
    body = e.c.body
    return (
        select(
            e.c.at,
            e.c.kind,
            body["toolCallId"].astext.label("call_id"),
            body["title"].astext.label("title"),
            body["kind"].astext.label("tool_kind"),
            body["status"].astext.label("status"),
        )
        .where(e.c.run_id == run_id, e.c.kind.in_(TOOL_EVENTS))
        .order_by(e.c.seq)
    )


async def record(conn: AsyncConnection, run_id: int) -> list[ToolFigures]:
    """Write the figures of run ``run_id`` from its events, in place of the ones it had; what was written."""
    r, s = tables.runs, tables.run_tool_stats
    runtime = (await conn.execute(select(r.c.runtime).where(r.c.id == run_id))).scalar_one()
    figures = summarize(runtime, (await conn.execute(_tool_events(run_id))).all())
    await conn.execute(delete(s).where(s.c.run_id == run_id))
    if figures:
        rows = [
            {
                "run_id": run_id,
                "tool_name": item.name,
                "calls": item.calls,
                "errors": item.errors,
                "duration_ms": item.duration_ms,
            }
            for item in figures
        ]
        await conn.execute(insert(s), rows)
    return figures


# Models


class ToolStat(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(alias=TOOL_NAME, description="the tool, as the module of tool_stats names it")
    calls: int
    errors: int = Field(description="calls an update of which said failed")
    duration_ms: int = Field(description="from each call to its result, added up; calls that never ended count none")


class RunToolStats(BaseModel):
    run_id: int
    runtime: str
    state: Literal[runs.RUN_STATES]
    finished_at: datetime | None
    tools: list[ToolStat] = Field(description="the most called first; empty for a run that has not ended")


class RuntimeToolStat(ToolStat):
    runtime: str
    runs: int = Field(description="runs that called the tool")


class ProjectToolStats(BaseModel):
    project: str
    days: int
    first_day: date
    last_day: date
    runs: int = Field(description="runs that ended in those days, with tool calls or without")
    tools: list[RuntimeToolStat] = Field(description="per tool and runtime, the most called first")


# Routes


@router.get("/{project}/runs/{run_id}/tool-stats", response_model=RunToolStats, responses=READ_REFUSALS)
async def run_tool_stats(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> RunToolStats:
    """What the tool calls of run ``run_id`` came to, per tool."""
    r, s = tables.runs, tables.run_tool_stats
    async with request.app.state.engine.begin() as conn:
        await readable_run(conn, user, project, run_id, sink)
        run = (await conn.execute(select(r.c.runtime, r.c.state, r.c.finished_at).where(r.c.id == run_id))).one()
        query = (
            select(s.c.tool_name, s.c.calls, s.c.errors, s.c.duration_ms)
            .where(s.c.run_id == run_id)
            .order_by(s.c.calls.desc(), s.c.tool_name)
        )
        rows = (await conn.execute(query)).all()
    tools = [
        ToolStat(name=row.tool_name, calls=row.calls, errors=row.errors, duration_ms=row.duration_ms) for row in rows
    ]
    return RunToolStats(run_id=run_id, runtime=run.runtime, state=run.state, finished_at=run.finished_at, tools=tools)


@router.get("/{project}/tool-stats", response_model=ProjectToolStats, responses=READ_REFUSALS)
async def project_tool_stats(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    days: Annotated[
        int, Query(ge=1, le=MAX_STATS_DAYS, description="the last days in UTC to count, today included")
    ] = STATS_DAYS,
    plan_id: Annotated[str | None, Query(pattern=plan_routes.PLAN_ID, description="the runs of this plan")] = None,
    runtime: Annotated[Literal[runs.RUNTIMES] | None, Query(description="the runs of this runtime")] = None,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> ProjectToolStats:
    """The tool figures of the runs of the plans the caller may read that ended in the last ``days`` days, per tool and
    runtime."""
    r, s = tables.runs, tables.run_tool_stats
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        plans = await visible_plans(conn, access, sink)
        today: date = (await conn.execute(select(cast(func.timezone("UTC", func.now()), Date)))).scalar_one()
        first = today - timedelta(days=days - 1)
        since = datetime.combine(first, time.min, tzinfo=UTC)
        until = datetime.combine(today + timedelta(days=1), time.min, tzinfo=UTC)
        ended = [
            r.c.project_id == access.project_id,
            r.c.plan_id.in_(plans),
            r.c.state.in_(runs.TERMINAL_STATES),
            r.c.finished_at >= since,
            r.c.finished_at < until,
        ]
        if plan_id is not None:
            ended.append(r.c.plan_id == plan_id)
        if runtime is not None:
            ended.append(r.c.runtime == runtime)
        counted = (await conn.execute(select(func.count()).select_from(r).where(*ended))).scalar_one()
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
        rows = (await conn.execute(query)).all()
    tools = [
        RuntimeToolStat(
            name=row.tool_name,
            runtime=row.runtime,
            calls=int(row.calls),
            errors=int(row.errors),
            duration_ms=int(row.duration_ms),
            runs=row.runs,
        )
        for row in rows
    ]
    return ProjectToolStats(project=access.name, days=days, first_day=first, last_day=today, runs=counted, tools=tools)
