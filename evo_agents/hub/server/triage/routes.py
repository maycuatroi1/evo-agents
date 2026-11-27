"""The triage routes of the worker protocol under /v1/worker: what the agent of a triage thread does on its own
(``triage.service.actions`` is the design, checks the frame and opens the transaction)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from evo_agents.hub.server.errors import CODES, ErrorBody
from evo_agents.hub.server.plans import REFUSALS as PLAN_REFUSALS
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.plans import service as plan_service
from evo_agents.hub.server.runs.models import REFUSALS, RunId
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.server.triage.models.actions import (
    TriageActed,
    TriageCancelIn,
    TriageCancelled,
    TriageInvited,
    TriageInviteIn,
    TriagePlanIn,
    TriageRerunIn,
)
from evo_agents.hub.server.triage.service import actions

log = logging.getLogger(__name__)

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


@worker_router.post("/runs/{run_id}/triage/rerun", status_code=201, response_model=TriageActed, responses={**REFUSALS})
async def rerun(request: Request, run_id: RunId, body: TriageRerunIn, user: CurrentUser) -> TriageActed:
    """Run the failed run of this triage thread again for its owner, inside the frame; 422 for a cause with no
    automatic rerun, 409 when the chain of reruns or an earlier rerun forbids it."""
    failed_id, acted = await actions.rerun(request.app.state.engine, user, run_id, body)
    log.info("triage rerun", extra={"run_id": failed_id, "rerun": acted.run_id, "agent_run_id": run_id})
    return acted


@worker_router.post("/runs/{run_id}/triage/cancel", response_model=TriageCancelled, responses={**REFUSALS})
async def cancel(request: Request, run_id: RunId, body: TriageCancelIn, user: CurrentUser) -> TriageCancelled:
    """Ask the worker of the stale run of this triage thread to stop it, for its owner, as their cancel of a held run
    does; 409 for the thread of a failed run, a run that is no longer held, or a stop asked already."""
    cancelled = await actions.cancel(request.app.state.engine, user, run_id, body)
    log.info("triage cancel", extra={"run_id": cancelled.run_id, "agent_run_id": run_id})
    return cancelled


@worker_router.post(
    "/runs/{run_id}/triage/plan",
    status_code=201,
    response_model=TriageActed,
    responses={**REFUSALS, **PLAN_REFUSALS},
)
async def edit_plan(request: Request, run_id: RunId, body: TriagePlanIn, user: CurrentUser):
    """Change the ``verify`` or narrow the ``what`` of pending steps of the failed run's plan for its owner, one step or
    several (``steps``), as one new revision under their name and one ``Decision:`` line, inside the frame
    (``triage_rules.edit_problems``); with ``rerun`` the failed step is run again in the same transaction, and a refused
    rerun leaves the plan as it was."""
    try:
        failed_id, acted = await actions.edit_plan(request.app.state.engine, user, run_id, body)
    except plan_models.PlanError as exc:
        return plan_service.refusal(request, exc)
    log.info("triage plan edit", extra={"run_id": failed_id, "revision": acted.plan_revision, "agent_run_id": run_id})
    return acted


def _refused(request: Request, status: int, message: str) -> JSONResponse:
    body = ErrorBody(error=CODES.get(status, f"http_{status}"), message=message)
    content = body.model_dump(mode="json", exclude_none=True)
    content["request_id"] = getattr(request.state, "request_id", None)
    return JSONResponse(content, status_code=status)


@worker_router.post(
    "/runs/{run_id}/triage/invite", status_code=201, response_model=TriageInvited, responses={**REFUSALS}
)
async def invite(request: Request, run_id: RunId, body: TriageInviteIn, user: CurrentUser):
    """Invite the agent ``handle`` of another project, one the failed run's error names, into the chat of this thread
    for the run's owner; 422 for an agent of no project the error names, 409 (said in the thread too) when the owner is
    no writer of its project or the chat's label or size forbids it."""
    failed_id, invited = await actions.invite(request.app.state.engine, user, run_id, body)
    reply = _refused(request, 409, invited) if isinstance(invited, str) else invited
    log.info("triage invite", extra={"run_id": failed_id, "handle": body.handle, "agent_run_id": run_id})
    return reply
