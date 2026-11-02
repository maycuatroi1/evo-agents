"""Reading a plan's runs: GET /v1/projects/{p}/plans/{plan}/ready-steps, every step of the plan with whether a
dispatch of it would be taken now, and why not; and GET /v1/projects/{p}/runs/{id}/rerun-candidates, who could run a
failed run again, or take a queued one now, with why each can or cannot."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request

from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import REFUSALS, ReadySteps, RunId
from evo_agents.hub.server.runs.service.candidates import RerunCandidates, rerun_candidates
from evo_agents.hub.server.runs.service.views import step_readiness
from evo_agents.hub.server.security import CurrentUser

router = APIRouter()


@router.get(
    "/{project}/plans/{plan_id}/ready-steps",
    response_model=ReadySteps,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def ready_steps(
    request: Request,
    project: ProjectName,
    plan_id: plan_routes.PlanId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> ReadySteps:
    """Every step of the plan with whether it may be dispatched now, and why not."""
    return await step_readiness(request.app.state.engine, user, project, plan_id, sink)


@router.get("/{project}/runs/{run_id}/rerun-candidates", response_model=RerunCandidates, responses=REFUSALS)
async def run_candidates(request: Request, project: ProjectName, run_id: RunId, user: CurrentUser) -> RerunCandidates:
    """The workers of the run's owner and the agent profiles of the project that could run a failed run again, each
    with why it fits or why not, the best first; for a queued run, the workers that could take it now as the claim
    counts it, the agent pools of the project and why no worker can (``diagnosis``). The owner's alone (403 for
    another member), 409 for a run neither failed nor queued."""
    return await rerun_candidates(request.app.state.engine, user, project, run_id)
