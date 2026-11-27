"""The routes of what a review run writes, and of a member's own session that records the same way, and of the
night's figures (``evo_agents.hub.server.proposals.service.record`` says what each does): POST
/v1/worker/runs/{id}/findings and .../proposals, POST /v1/projects/{p}/curator/findings and .../proposals, and GET
.../curator/figures."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.proposals.models import Finding, FindingIn, NightFiguresView, SessionFindingIn
from evo_agents.hub.server.proposals.service import record as service
from evo_agents.hub.server.proposals.service.record import held_review_run, night_only, session_review_run
from evo_agents.hub.server.proposals.service.shapes import Proposal, ProposalIn, SessionProposalIn
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.security import CurrentUser

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
READ_REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404)}


# Findings


@worker_router.post("/runs/{run_id}/findings", status_code=201, response_model=Finding, responses=REFUSALS)
async def record_finding(request: Request, run_id: RunId, body: FindingIn, user: CurrentUser) -> Finding:
    """Record a finding of the review run this worker holds, with evidence the hub finds."""
    return await service.record_finding(request, body, lambda conn: held_review_run(conn, user, run_id))


@router.post("/{project}/curator/findings", status_code=201, response_model=Finding, responses=REFUSALS)
async def record_session_finding(
    request: Request, project: ProjectName, body: SessionFindingIn, user: CurrentUser
) -> Finding:
    """Record a finding of your own session, in its local run, as a review run's agent records one."""
    night_only(body)
    return await service.record_finding(
        request, body, lambda conn: session_review_run(conn, user, project, body.run_id)
    )


# Proposals


@worker_router.post("/runs/{run_id}/proposals", status_code=201, response_model=Proposal, responses=REFUSALS)
async def record_proposal(request: Request, run_id: RunId, body: ProposalIn, user: CurrentUser) -> Proposal:
    """Record a proposal of the review run this worker holds: the hub resolves its evidence, checks its draft plan,
    computes its tier and drops it when it repeats a proposal rejected lately."""
    return await service.record_proposal(request, body, lambda conn: held_review_run(conn, user, run_id))


@router.post("/{project}/curator/proposals", status_code=201, response_model=Proposal, responses=REFUSALS)
async def record_session_proposal(
    request: Request, project: ProjectName, body: SessionProposalIn, user: CurrentUser
) -> Proposal:
    """Record a proposal of your own session, in its local run, as a review run's agent records one: the same
    evidence, draft plan, tier and repeat checks."""
    night_only(body)
    return await service.record_proposal(
        request, body, lambda conn: session_review_run(conn, user, project, body.run_id)
    )


# Reading


@router.get("/{project}/curator/figures", response_model=NightFiguresView, responses=READ_REFUSALS)
async def show_figures(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    night: Annotated[date | None, Query(description="the night, the local date its window opened; the latest")] = None,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> NightFiguresView:
    """The figures curator.collect counted for a night of the project: the latest, or ``night``'s."""
    return await service.night_figures(request.app.state.engine, user, project, night, sink)
