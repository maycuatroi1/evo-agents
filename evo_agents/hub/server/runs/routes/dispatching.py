"""Dispatching runs: POST /v1/projects/{p}/runs (the runs of a plan's steps), POST .../plan-runs (a plan run) and POST
.../author-runs (an author run), each to a worker of the caller's (``service.dispatch``)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import REFUSALS, AuthorRunDispatch, Dispatch, PlanRunDispatch, Run
from evo_agents.hub.server.runs.service.dispatch import dispatch_author_run, dispatch_plan_run, dispatch_steps
from evo_agents.hub.server.security import CurrentUser

router = APIRouter()


@router.post(
    "/{project}/runs",
    status_code=201,
    response_model=list[Run],
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def dispatch(request: Request, project: ProjectName, body: Dispatch, user: CurrentUser) -> list[Run]:
    """Queue a run of each step named, all of them or none; each one goes to a worker of the caller."""
    return await dispatch_steps(request.app.state.engine, user, project, body)


@router.post(
    "/{project}/plan-runs",
    status_code=201,
    response_model=Run,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def dispatch_plan(request: Request, project: ProjectName, body: PlanRunDispatch, user: CurrentUser) -> Run:
    """Queue a plan run: one run, on a worker of the caller's, that does every step of the plan not done yet."""
    return await dispatch_plan_run(request.app.state.engine, user, project, body)


@router.post(
    "/{project}/author-runs",
    status_code=201,
    response_model=Run,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def dispatch_author(request: Request, project: ProjectName, body: AuthorRunDispatch, user: CurrentUser) -> Run:
    """Queue an author run: a plan written from the caller's request, with create-exec-plan, on a worker of the
    caller's (``evo_agents.hub.author``)."""
    return await dispatch_author_run(request.app.state.engine, user, project, body)
