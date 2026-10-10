"""A plan run's plan and its steps, and an author run's plan: GET and PUT /v1/worker/runs/{id}/plan and POST
/v1/worker/runs/{id}/steps/{key} (``service.plan_runs``)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import REFUSALS, AuthoredPlan, RunId, StepKey, StepReport, StepWritten
from evo_agents.hub.server.runs.service.plan_runs import put_authored_plan, run_plan, write_step_report
from evo_agents.hub.server.security import CurrentUser

router = APIRouter()


@router.get("/runs/{run_id}/plan", response_model=plan_routes.Plan, responses=REFUSALS)
async def read_run_plan(request: Request, run_id: RunId, user: CurrentUser):
    """The plan of a plan run or an author run this worker holds, as the hub holds it now, read as the member who
    dispatched the run; 404 for an author run that has written no plan yet and was dispatched on none."""
    return await run_plan(request.app.state.engine, user, run_id)


@router.put("/runs/{run_id}/plan", response_model=plan_routes.Written, responses=plan_routes.REFUSALS)
async def put_run_plan(request: Request, run_id: RunId, payload: AuthoredPlan, user: CurrentUser):
    """Put the plan an author run this worker holds wrote on the hub: in the run's project, as the member who
    dispatched the run, whose writer role is checked again now, with the checks of PUT /v1/projects/{p}/plans/{id}
    (``plans.write_plan``); the revision records the run. The run writes one plan, the one it was dispatched on or the
    one its first write created (422 for another id), never replaces a plan without ``if_revision`` (409), and never
    changes its progress (422, ``author.progress_problem``)."""
    try:
        return await put_authored_plan(request.app.state.engine, user, run_id, payload)
    except plan_routes.PlanError as exc:
        return plan_routes._refusal(request, exc)


@router.post(
    "/runs/{run_id}/steps/{key}", response_model=StepWritten, responses={**REFUSALS, 422: {"model": ErrorBody}}
)
async def report_step(request: Request, run_id: RunId, key: StepKey, body: StepReport, user: CurrentUser):
    """Write a step of the plan of a plan run this worker holds, as the member who dispatched the run."""
    return await write_step_report(request.app.state.engine, user, run_id, key, body)
