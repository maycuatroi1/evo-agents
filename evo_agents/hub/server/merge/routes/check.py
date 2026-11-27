"""The worker routes of the merge check of a pull request on github.com (``service.check``): asking for it, the gate
of the agent's merge command, and the agent of a held check's thread."""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.merge.models.check import (
    MergeCheck,
    MergeCheckIn,
    MergeGate,
    MergeGateIn,
    MergeTriaged,
    MergeTriageIn,
)
from evo_agents.hub.server.merge.service import check
from evo_agents.hub.server.runs.models import REFUSALS, RunId
from evo_agents.hub.server.security import CurrentUser

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


@worker_router.post(
    "/runs/{run_id}/merge-check",
    response_model=MergeCheck,
    responses={**REFUSALS, 409: {"model": ErrorBody}, 422: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)
async def ask_check(request: Request, run_id: RunId, body: MergeCheckIn, user: CurrentUser) -> MergeCheck:
    """Have the hub judge the run's pull request on github.com at its head before the run merges it: where the check
    stands, and the wait of the run its verdict answers while it judges."""
    app = getattr(request.app.state, "github_app", None)
    return await check.ask(request.app.state.engine, app, user, run_id, body)


@worker_router.post(
    "/runs/{run_id}/merge-gate", response_model=MergeGate, responses={**REFUSALS, 422: {"model": ErrorBody}}
)
async def merge_gate_act(request: Request, run_id: RunId, body: MergeGateIn, user: CurrentUser) -> MergeGate:
    """The word of the hub on a merge command of the run's agent (K5): whether the pull request's repo has the merge
    check on, and when it has, whether a passed verdict holds for the head the command merges."""
    return await check.gate(request.app.state.engine, user, run_id, body)


@worker_router.post(
    "/runs/{run_id}/triage/merge-check",
    response_model=MergeTriaged,
    responses={**REFUSALS, 409: {"model": ErrorBody}, 422: {"model": ErrorBody}},
)
async def triage_check(request: Request, run_id: RunId, body: MergeTriageIn, user: CurrentUser) -> MergeTriaged:
    """In the thread of a merge check its triage holds (``merge_holds``), for the plan run's owner: judge it again, or
    hand its failure back to the plan run; 403 for another member's run, 404 outside such a thread, 409 for a check no
    longer held."""
    return await check.triage(request.app.state.engine, user, run_id, body)
