"""The evolve routes of the Curator: each hands the request's parts and the app's engine to the service
(``service.start``, ``service.views``, ``service.worker``), which opens the transaction; the package's docstring says
what they do."""

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
from evo_agents.hub.server.plans import models as plan_models
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
    return await start.start_evolution(request.app.state.engine, user, project, body)


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
    return await views.listed(request.app.state.engine, user, project, sink, state=state, limit=limit)


@router.get("/{project}/curator/evolve/{evolve_id}", response_model=EvolveView, responses=REFUSALS)
async def show_evolution(
    request: Request, project: ProjectName, evolve_id: EvolveId, user: CurrentUser, sink: Sink = None
) -> EvolveView:
    """One evolution, with each candidate (its components, scores, cost and ASI of each task) and its front."""
    return await views.shown(request.app.state.engine, user, project, evolve_id, sink)


# The worker of the run


@worker_router.get("/runs/{run_id}/evolve", response_model=EvolveInputs, responses=REFUSALS)
async def evolve_inputs(request: Request, run_id: RunId, user: CurrentUser, evolve_key: EvolveKey = None):
    """What the held evolve run evolves: its baseline, its light tasks with their verify, the cached baseline arms."""
    return await worker.inputs(request.app.state.engine, user, run_id, evolve_key)


@worker_router.post("/runs/{run_id}/evolve/arm", response_model=ArmOut, responses=REFUSALS)
async def evolve_arm(
    request: Request, run_id: RunId, body: ArmIn, user: CurrentUser, evolve_key: EvolveKey = None
) -> ArmOut:
    """Keep the result of a baseline arm of one task by its ``evolve.arm_key``; one the cache holds stays as it is."""
    return await worker.arm(request.app.state.engine, user, run_id, evolve_key, body)


@worker_router.post("/runs/{run_id}/evolve/candidate", response_model=CandidateOut, responses=REFUSALS)
async def evolve_candidate(
    request: Request, run_id: RunId, body: CandidateIn, user: CurrentUser, evolve_key: EvolveKey = None
) -> CandidateOut:
    """Keep a candidate, unique by the digest of its components, and recompute the evolution's Pareto front."""
    return await worker.candidate(request.app.state.engine, user, run_id, evolve_key, body)


@worker_router.post("/runs/{run_id}/evolve/report", response_model=EvolveView, responses=REFUSALS)
async def evolve_report_route(
    request: Request, run_id: RunId, body: ReportIn, user: CurrentUser, evolve_key: EvolveKey = None
) -> EvolveView:
    """End the evolution: of target git, its branch goes on as its proposal's change; of target agent_config, its best
    candidate is pinned as the agent's next version when it holds on the holdout split; why, either way."""
    found = await worker.end(request.app.state.engine, user, run_id, evolve_key, body)
    log.info("evolution reported", extra={"run_id": run_id, "evolve_id": found.id, "state": found.state})
    return found
