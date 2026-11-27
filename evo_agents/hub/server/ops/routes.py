"""The routes of the ops actions of an Orchestrator in an ops thread (``ops.service.actions`` is the design): each takes
the worker token of the worker that holds the chat run, locks the run, and hands the connection to the service."""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.chats.models.agents import REFUSALS
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.ops.models.actions import (
    OpsActed,
    OpsFindingIn,
    OpsPlanIn,
    OpsPriorityIn,
    OpsReadIn,
    OpsReasonIn,
    OpsRunIn,
    OpsWaitIn,
)
from evo_agents.hub.server.ops.service.actions import (
    answer_wait,
    curator_resume,
    finding,
    held,
    plan_dispatch,
    priority,
    read,
    rerun,
)
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.security import CurrentUser

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})

_RESPONSES = {403: {"model": ErrorBody}, **REFUSALS}


@worker_router.post("/runs/{run_id}/ops/rerun", response_model=OpsActed, responses=_RESPONSES)
async def rerun_route(request: Request, run_id: RunId, body: OpsRunIn, user: CurrentUser) -> OpsActed:
    """Run the work of a failed run of this ops thread's batch again, as its Orchestrator's chat run."""
    async with request.app.state.engine.begin() as conn:
        return await rerun(conn, await held(conn, user, run_id), body)


@worker_router.post("/runs/{run_id}/ops/redeliver", response_model=OpsActed, responses=_RESPONSES)
async def redeliver_route(request: Request, run_id: RunId, body: OpsRunIn, user: CurrentUser) -> OpsActed:
    """Hand the work of a failed run of this ops thread's batch to a healthy worker, as its Orchestrator's chat run."""
    async with request.app.state.engine.begin() as conn:
        return await rerun(conn, await held(conn, user, run_id), body, redeliver=True)


@worker_router.post("/runs/{run_id}/ops/answer-wait", response_model=OpsActed, responses=_RESPONSES)
async def answer_wait_route(request: Request, run_id: RunId, body: OpsWaitIn, user: CurrentUser) -> OpsActed:
    """Answer a stuck wait of this ops thread's batch, as its Orchestrator's chat run."""
    async with request.app.state.engine.begin() as conn:
        return await answer_wait(conn, await held(conn, user, run_id), body)


@worker_router.post("/runs/{run_id}/ops/curator-resume", response_model=OpsActed, responses=_RESPONSES)
async def curator_resume_route(request: Request, run_id: RunId, body: OpsReasonIn, user: CurrentUser) -> OpsActed:
    """Resume the Curator's schedules its breaker paused, as the Orchestrator's chat run of an ops thread."""
    async with request.app.state.engine.begin() as conn:
        return await curator_resume(conn, await held(conn, user, run_id), body)


@worker_router.post("/runs/{run_id}/ops/read", response_model=OpsActed, responses=_RESPONSES)
async def read_route(request: Request, run_id: RunId, body: OpsReadIn, user: CurrentUser) -> OpsActed:
    """Mark a notice of the project read, as the Orchestrator's chat run of an ops thread."""
    async with request.app.state.engine.begin() as conn:
        return await read(conn, await held(conn, user, run_id), body)


@worker_router.post("/runs/{run_id}/ops/finding", response_model=OpsActed, responses=_RESPONSES)
async def finding_route(request: Request, run_id: RunId, body: OpsFindingIn, user: CurrentUser) -> OpsActed:
    """Write a finding for the Curator, as the Orchestrator's chat run of an ops thread."""
    async with request.app.state.engine.begin() as conn:
        return await finding(conn, await held(conn, user, run_id), body)


@worker_router.post("/runs/{run_id}/ops/priority", response_model=OpsActed, responses=_RESPONSES)
async def priority_route(request: Request, run_id: RunId, body: OpsPriorityIn, user: CurrentUser) -> OpsActed:
    """Give a queued run of the project another priority, as the Orchestrator's chat run of an ops thread."""
    async with request.app.state.engine.begin() as conn:
        return await priority(conn, await held(conn, user, run_id), body)


@worker_router.post("/runs/{run_id}/ops/plan-dispatch", response_model=OpsActed, responses=_RESPONSES)
async def plan_dispatch_route(request: Request, run_id: RunId, body: OpsPlanIn, user: CurrentUser) -> OpsActed:
    """Hand a stalled plan of this ops thread's batch again to the default system's entry agent, as its Orchestrator's
    chat run."""
    public_url = request.app.state.config.public_url
    async with request.app.state.engine.begin() as conn:
        return await plan_dispatch(conn, await held(conn, user, run_id), body, public_url=public_url)
