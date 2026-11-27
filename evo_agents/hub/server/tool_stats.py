"""What the tool calls of a run came to: per tool, the calls, the calls that failed and the time they took. The hub
writes them when the run ends, so they outlive the run's events, which hub.prune_run_events deletes after
EVO_HUB_RUN_LOG_DAYS.

``record`` reads the run's tool_call and tool_call_update events (``docs/workers.md``, "Event kinds") and replaces the
run's rows of run_tool_stats with what ``summarize`` makes of them. ``run_state.move_run`` calls it in the transaction
of every move to an end state; ``run_events.post_events`` calls it again for events that arrive after the end, since a
worker's spool may send its last ones then; and ``run_reaper.prune_run_events`` calls it just before it deletes a run's
events, so a run that ended before schema 0013 keeps its figures too.

A call is a tool_call event, matched to its updates by ``toolCallId``. It failed when an update says ``failed``, and
it took the time from its tool_call to its first update that says ``completed`` or ``failed``, by the times the worker
gave the events; a call that never finished counts no time. The tool's name, ``gen_ai.tool.name`` as OpenTelemetry's
GenAI conventions call it, is the title Claude Code gives a call, which is its tool's name. Codex and opencode title a
call by what it does (the command, the file), so for them the name is the call's kind (execute, edit, read, search,
fetch, think), and the title for a call of kind other, which is an MCP tool (``server/tool`` for Codex).

The calls of Claude Code's Skill tool also name the skill each loaded (the call's ``rawInput.skill``): the Skill
row keeps per skill its calls and the calls that failed (run_tool_stats.skills, schema 0087), and the routes list them
as ``skills``. Runs that ended before 0087 have none.

GET /v1/projects/{project}/runs/{run_id}/tool-stats shows one run's figures to whoever may read the run
(``runs.readable_run``). GET /v1/projects/{project}/tool-stats adds them up per tool and runtime over the runs of the
plans the caller may read that ended on the last ``days`` days in UTC, today included, filtered by plan and runtime.
The MCP tool run_tool_stats answers both.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Request

from evo_agents.hub import runs
from evo_agents.hub.plans import PLAN_ID
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.runs.models.tool_stats import MAX_STATS_DAYS, STATS_DAYS
from evo_agents.hub.server.runs.models.tool_stats import ProjectToolStats as ProjectToolStats
from evo_agents.hub.server.runs.models.tool_stats import RuntimeSkillStat as RuntimeSkillStat
from evo_agents.hub.server.runs.models.tool_stats import RuntimeToolStat as RuntimeToolStat
from evo_agents.hub.server.runs.models.tool_stats import RunToolStats as RunToolStats
from evo_agents.hub.server.runs.models.tool_stats import SkillStat as SkillStat
from evo_agents.hub.server.runs.models.tool_stats import ToolFigures as ToolFigures
from evo_agents.hub.server.runs.models.tool_stats import ToolStat as ToolStat
from evo_agents.hub.server.runs.service import tool_stats as service
from evo_agents.hub.server.runs.service.tool_stats import (  # noqa: F401 (run_state, run_events, run_reaper, tests)
    record,
    summarize,
    tool_name,
)
from evo_agents.hub.server.security import CurrentUser

READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}

router = APIRouter(prefix="/v1/projects", tags=["runs"], responses={401: {"model": ErrorBody}})


@router.get("/{project}/runs/{run_id}/tool-stats", response_model=RunToolStats, responses=READ_REFUSALS)
async def run_tool_stats(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> RunToolStats:
    """What the tool calls of run ``run_id`` came to, per tool."""
    return await service.run_tool_stats(request.app.state.engine, user, project, run_id, sink)


@router.get("/{project}/tool-stats", response_model=ProjectToolStats, responses=READ_REFUSALS)
async def project_tool_stats(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    days: Annotated[
        int, Query(ge=1, le=MAX_STATS_DAYS, description="the last days in UTC to count, today included")
    ] = STATS_DAYS,
    plan_id: Annotated[str | None, Query(pattern=PLAN_ID, description="the runs of this plan")] = None,
    runtime: Annotated[
        str | None, Query(pattern=runs.RUNTIME_NAME, description="the runs of this runtime, of the core or a plugin's")
    ] = None,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> ProjectToolStats:
    """The tool figures of the runs of the plans the caller may read that ended in the last ``days`` days, per tool and
    runtime."""
    return await service.project_tool_stats(request.app.state.engine, user, project, days, plan_id, runtime, sink)
