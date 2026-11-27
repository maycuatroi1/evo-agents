"""Run plan on a Curator's plan: POST /v1/projects/{p}/curator/plans/{plan_id}/build (writer) queues the Builder of its
change now, beside the night shift's other runs, instead of waiting for them to end. It is the night shift's Builder in
every other way (``changes.queue_builder``, through ``curator.project_gate``): pinned to the worker on duty, or placed
on a ready node of the pool on duty (a pool with none is refused, naming why each node is not ready), as the
schedule's owner, on the change's branch alone, with the charter's runtime and the caps of the night that last opened,
at any hour; never while the night shift is paused or its circuit breaker tripped, past ``max_runs_per_night`` or the
night's budget, while a run of the plan is active, or before the hub checked the repo's ruleset. Each refusal is 409
with why, and nothing is queued. The run's events and the audit (curator.build_now) name the member who asked.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plans.models import PlanId
from evo_agents.hub.server.runs.models import Run
from evo_agents.hub.server.runs.service import build_now as service
from evo_agents.hub.server.runs.service.build_now import GATE_REFUSALS as GATE_REFUSALS
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})


@router.post(
    "/{project}/curator/plans/{plan_id}/build",
    status_code=201,
    response_model=Run,
    responses={code: {"model": ErrorBody} for code in (403, 404, 409)},
)
async def build(request: Request, project: ProjectName, plan_id: PlanId, user: CurrentUser) -> Run:
    """Queue the Builder of the Curator's plan ``plan_id`` now, beside the night shift's other runs."""
    curator_app = request.app.state.curator_app is not None
    return await service.build(request.app.state.engine, user, project, plan_id, curator_app=curator_app)
