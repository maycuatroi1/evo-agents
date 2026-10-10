"""Reading a plan's runs: GET /v1/projects/{p}/plans/{plan}/ready-steps, every step of the plan with whether a
dispatch of it would be taken now, and why not."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request

from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import ReadySteps
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
