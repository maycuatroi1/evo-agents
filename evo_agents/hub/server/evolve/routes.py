"""The evolve routes of the Curator: each opens the request's transaction and hands the request to the service
(``service.start``, ``service.views``, ``service.worker``); the package's docstring says what they do."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Header, Path, Query, Request

from evo_agents.hub import evolve
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.evolve.models import (
    MAX_LISTED,
    ArmIn,
    ArmOut,
    CandidateIn,
    CandidateOut,
    EvolveInputs,
    EvolveList,
    EvolveStart,
    EvolveView,
    ReportIn,
)
from evo_agents.hub.server.evolve.service import start, views, worker
from evo_agents.hub.server.forge.service.protection import need_reader
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.runs.models import MAX_ID, RunId
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}

EvolveKey = Annotated[
    str | None,
    Header(
        alias=evolve.EVOLVE_KEY_HEADER,
        max_length=200,
        description="the evolve run's own key, which its claim handed the daemon; the worker token alone is refused",
    ),
]
Sink = Annotated[str | None, Header(alias=plan_models.SINK_HEADER)]
EvolveId = Annotated[int, Path(ge=1, le=MAX_ID)]


# The owner


@router.post("/{project}/curator/evolve", status_code=201, response_model=EvolveView, responses=REFUSALS)
async def start_evolution(request: Request, project: ProjectName, body: EvolveStart, user: CurrentUser) -> EvolveView:
    """Start an evolution of a harness's files in a repo (git) or of an agent's configuration (agent_config)."""
    async with request.app.state.engine.begin() as conn:
        evolve_id = await start.start(conn, user, project, body)
        return await views.started(conn, project, evolve_id)


@router.get("/{project}/curator/evolve", response_model=EvolveList, responses=REFUSALS)
async def list_evolutions(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    state: Annotated[str | None, Query(pattern="^(queued|running|done|failed)$")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LISTED)] = 20,
    sink: Sink = None,
) -> EvolveList:
    """The project's evolutions, the newest first, each with its state, tasks and front."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        rows = await views.evolutions_of(conn, access, sink, state=state, limit=limit)
    return EvolveList(project=access.name, evolutions=[views.view_of(access.name, row) for row in rows])


@router.get("/{project}/curator/evolve/{evolve_id}", response_model=EvolveView, responses=REFUSALS)
async def show_evolution(
    request: Request, project: ProjectName, evolve_id: EvolveId, user: CurrentUser, sink: Sink = None
) -> EvolveView:
    """One evolution, with each candidate (its components, scores, cost and ASI of each task) and its front."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        row = await views.evolution_of(conn, access, evolve_id, sink)
        candidates = await views.candidates_of(conn, row.id, components=True)
    return views.view_of(access.name, row, candidates)


# The worker of the run


@worker_router.get("/runs/{run_id}/evolve", response_model=EvolveInputs, responses=REFUSALS)
async def evolve_inputs(request: Request, run_id: RunId, user: CurrentUser, evolve_key: EvolveKey = None):
    """What the held evolve run evolves: its baseline, its light tasks with their verify, the cached baseline arms."""
    async with request.app.state.engine.begin() as conn:
        row = await worker.held_evolve(conn, user, run_id, evolve_key)
        return await worker.inputs_of(conn, row)


@worker_router.post("/runs/{run_id}/evolve/arm", response_model=ArmOut, responses=REFUSALS)
async def evolve_arm(
    request: Request, run_id: RunId, body: ArmIn, user: CurrentUser, evolve_key: EvolveKey = None
) -> ArmOut:
    """Keep the result of a baseline arm of one task by its ``evolve.arm_key``; one the cache holds stays as it is."""
    async with request.app.state.engine.begin() as conn:
        row = await worker.held_evolve(conn, user, run_id, evolve_key)
        return await worker.kept_arm(conn, row, run_id, body)


@worker_router.post("/runs/{run_id}/evolve/candidate", response_model=CandidateOut, responses=REFUSALS)
async def evolve_candidate(
    request: Request, run_id: RunId, body: CandidateIn, user: CurrentUser, evolve_key: EvolveKey = None
) -> CandidateOut:
    """Keep a candidate, unique by the digest of its components, and recompute the evolution's Pareto front."""
    async with request.app.state.engine.begin() as conn:
        row = await worker.held_evolve(conn, user, run_id, evolve_key)
        return await worker.kept_candidate(conn, row, body)


@worker_router.post("/runs/{run_id}/evolve/report", response_model=EvolveView, responses=REFUSALS)
async def evolve_report_route(
    request: Request, run_id: RunId, body: ReportIn, user: CurrentUser, evolve_key: EvolveKey = None
) -> EvolveView:
    """End the evolution: of target git, its branch goes on as its proposal's change; of target agent_config, its best
    candidate is pinned as the agent's next version when it holds on the holdout split; why, either way."""
    async with request.app.state.engine.begin() as conn:
        row = await worker.held_evolve(conn, user, run_id, evolve_key)
        project, row = await worker.reported(conn, user, row, run_id, body)
    log.info("evolution reported", extra={"run_id": run_id, "evolve_id": row.id, "state": row.state})
    return views.view_of(project, row)
