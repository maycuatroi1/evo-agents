"""The routes of the Curator's replays (see the package): the worker's report of a replay run, the project's suite of
replay tasks and a change's replays. Each opens the request's transaction and hands it to the service."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Header, Path, Query, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.replays import service
from evo_agents.hub.server.replays.models import (
    MAX_TASKS_LISTED,
    REFUSALS,
    ChangeReplay,
    Replay,
    ReplayKey,
    ReplayReport,
    ReplayTaskList,
    Split,
)
from evo_agents.hub.server.runs.models import MAX_ID, RunId
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


@worker_router.post("/runs/{run_id}/replay", response_model=Replay, responses=REFUSALS)
async def report_replay(
    request: Request, run_id: RunId, body: ReplayReport, user: CurrentUser, replay_key: ReplayKey = None
) -> Replay:
    """One arm's result of the replay run this worker holds, or why it cannot replay its task; with the run's key."""
    async with request.app.state.engine.begin() as conn:
        found, replay_id = await service.report(conn, user, run_id, body, replay_key)
    log.info("replay reported", extra={"run_id": run_id, "replay_id": replay_id, "arm": body.arm})
    return found


@router.get("/{project}/curator/replay/tasks", response_model=ReplayTaskList, responses=REFUSALS)
async def list_tasks(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    split: Annotated[Split | None, Query(description="the tasks of this split alone")] = None,  # pyright: ignore[reportInvalidTypeForm]
    limit: Annotated[int, Query(ge=1, le=MAX_TASKS_LISTED)] = 100,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> ReplayTaskList:
    """The project's suite of replay tasks, in the order they came in, of the plans the caller may read."""
    async with request.app.state.engine.begin() as conn:
        access = await service.reader_access(conn, user, project)
        tasks, counts, total = await service.tasks_of(conn, access, split=split, limit=limit, sink=sink)
    return ReplayTaskList(project=access.name, split=split, tasks=tasks, total=total, counts=counts)


@router.get("/{project}/curator/changes/{change_id}/replay", response_model=ChangeReplay, responses=REFUSALS)
async def show_change_replay(
    request: Request,
    project: ProjectName,
    change_id: Annotated[int, Path(ge=1, le=MAX_ID)],
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> ChangeReplay:
    """A change's replays: each task, what arm a (the default branch's harness) and arm b (the change's) found."""
    async with request.app.state.engine.begin() as conn:
        access = await service.reader_access(conn, user, project)
        return await service.change_replay(conn, access, change_id, sink)
