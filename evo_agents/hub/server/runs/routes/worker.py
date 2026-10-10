"""The worker's side of the queue: POST /v1/worker/claim, POST /v1/worker/heartbeat and POST
/v1/worker/runs/{id}/state (``service.claims``, ``service.reports``)."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Request

from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import (
    REFUSALS,
    Claim,
    ClaimRequest,
    HeartbeatAnswer,
    HeartbeatRequest,
    Run,
    RunId,
    StateReport,
)
from evo_agents.hub.server.runs.service.claims import wait_for_run
from evo_agents.hub.server.runs.service.reports import apply_state_report, record_heartbeat
from evo_agents.hub.server.security import CurrentUser

router = APIRouter()


def lease_of(request: Request) -> timedelta:
    """How long a claim and each heartbeat lease a run for: EVO_HUB_RUN_LEASE_SECONDS."""
    return timedelta(seconds=request.app.state.config.run_lease_seconds)


@router.post("/claim", response_model=Claim, responses={403: {"model": ErrorBody}})
async def claim(request: Request, user: CurrentUser, body: ClaimRequest | None = None) -> Claim:
    """Wait up to ``wait_s`` seconds (25 by default) for a run this worker may take, and lease it."""
    wait = (body or ClaimRequest()).wait_s
    state = request.app.state
    return await wait_for_run(
        state.engine, state.run_wakeups, user, lease_of(request), wait, request.is_disconnected, state.blobs
    )


@router.post("/heartbeat", response_model=HeartbeatAnswer, responses={403: {"model": ErrorBody}})
async def heartbeat(request: Request, body: HeartbeatRequest, user: CurrentUser) -> HeartbeatAnswer:
    """Record the machine, extend the leases of the runs it holds, and say what it should do with them."""
    state = request.app.state
    return await record_heartbeat(state.engine, user, body, lease_of(request), state.terminals)


@router.post("/runs/{run_id}/state", response_model=Run, responses=REFUSALS)
async def report_state(request: Request, run_id: RunId, body: StateReport, user: CurrentUser) -> Run:
    """Move a run this worker holds, as the transition table lets a worker."""
    return await apply_state_report(request.app.state, user, run_id, body)
