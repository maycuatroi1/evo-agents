"""Listing and showing runs: GET /v1/projects/{p}/runs and GET .../runs/{id}, with GET .../runs/stats between them
(``routes.stats``), since GET .../runs/{id} would take ``stats`` for a run id and refuse it."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Request

from evo_agents.hub import runs
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import MAX_ID, MAX_LIST, MAX_OFFSET, STEP_KEY_CHARS, Run, RunId, RunList
from evo_agents.hub.server.runs.routes import stats
from evo_agents.hub.server.runs.service.listing import run_list, shown_run
from evo_agents.hub.server.security import CurrentUser

router = APIRouter()


@router.get("/{project}/runs", response_model=RunList, responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}})
async def list_runs(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    state: Annotated[list[Literal[runs.RUN_STATES]], Query(description="any of these states; repeat it")] = [],  # noqa: B006
    plan_id: Annotated[str | None, Query(pattern=plan_routes.PLAN_ID)] = None,
    step: Annotated[str | None, Query(min_length=1, max_length=STEP_KEY_CHARS, description="a step key")] = None,
    worker_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
    dispatched_by: Annotated[str | None, Query(min_length=1, max_length=100, description="a login")] = None,
    q: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=200,
            description="text in the title, step, plan, repo, branch, worker, login or error, or a run number",
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> RunList:
    """The project's runs, newest first, of the plans the caller may read, with how many are in each state."""
    return await run_list(
        request.app.state.engine,
        user,
        project,
        state=state,
        plan_id=plan_id,
        step=step,
        worker_id=worker_id,
        dispatched_by=dispatched_by,
        q=q,
        limit=limit,
        offset=offset,
        sink=sink,
    )


router.include_router(stats.router)  # before GET .../runs/{run_id}, which would take "stats" for a run id


@router.get(
    "/{project}/runs/{run_id}",
    response_model=Run,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def show_run(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> Run:
    return await shown_run(request.app.state.engine, user, project, run_id, sink)
