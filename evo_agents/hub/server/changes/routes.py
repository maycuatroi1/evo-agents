"""The routes of the Curator's changes (``evo_agents.hub.server.changes.service.job`` describes the design): what a
judge run reads and the verdict it posts, with the run's own key (``service.judge``), the list of a project's changes,
an admin's close of a change with no pull request, and the drop of a change by its own Builder before its
pull request opens (``service.manage``)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Request, Response

from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.changes.models.views import (
    Change,
    ChangeClose,
    ChangeDrop,
    ChangeList,
    JudgeInputs,
    JudgeKey,
    VerdictIn,
)
from evo_agents.hub.server.changes.service import judge, manage
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import MAX_ID, RunId
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409)}

ChangeId = Annotated[int, Path(ge=1, le=MAX_ID)]


# The judge run's side


@worker_router.get("/runs/{run_id}/judge", response_model=JudgeInputs, responses=REFUSALS)
async def judge_inputs(
    request: Request, run_id: RunId, user: CurrentUser, response: Response, judge_key: JudgeKey = None
) -> JudgeInputs:
    """What the judge run this worker holds reads: its change, its proposal, the plan's verify commands, the
    charter's protected paths and the project's hidden checks; with the run's own key."""
    found = await judge.judge_inputs(request.app.state.engine, user, run_id, judge_key)
    response.headers.update(NO_STORE)
    return found


@worker_router.post("/runs/{run_id}/verdict", response_model=Change, responses=REFUSALS)
async def record_verdict(
    request: Request, run_id: RunId, body: VerdictIn, user: CurrentUser, judge_key: JudgeKey = None
) -> Change:
    """The verdict of the judge run this worker holds, with the run's own key: the hub passes the change only as
    ``judge.final_verdict`` says, and the paths the diff touches give its proposal its tier again. The graph read
    for that tier can wait minutes for the project's snapshot to load, so it is read before the transaction that locks
    the run and its change: a request holding those locks makes the worker's heartbeat, claim and events wait on
    them, each holding a pool connection, until the pool is out."""
    return await judge.record_verdict(request.app.state, user, run_id, body, judge_key)


# Reading


@router.get("/{project}/curator/changes", response_model=ChangeList, responses=REFUSALS)
async def list_changes(request: Request, project: ProjectName, user: CurrentUser) -> ChangeList:
    """The project's Curator changes, newest first, with the plans the caller may read."""
    return await manage.list_changes(request.app.state.engine, user, project)


# Closing by hand


@router.post("/{project}/curator/changes/{change_id}/close", response_model=Change, responses=REFUSALS)
async def close_change(
    request: Request, project: ProjectName, change_id: ChangeId, body: ChangeClose, user: CurrentUser
) -> Change:
    """Close a change with no pull request, left open or planned with no active run, saying why; admins only."""
    return await manage.close_change(request.app.state.engine, user, project, change_id, body)


# Dropped by its Builder


@worker_router.post(
    "/runs/{run_id}/curator/drop", response_model=Change, responses={**REFUSALS, 422: {"model": ErrorBody}}
)
async def drop_change(request: Request, run_id: RunId, body: ChangeDrop, user: CurrentUser) -> Change:
    """Close the change whose plan the plan run this worker holds builds, before its pull request opens, saying why,
    for the member who dispatched the run, an admin of the project; with ``note``, what the run measured, kept in the
    ledger line."""
    return await manage.drop_change(request.app.state.engine, user, run_id, body)
