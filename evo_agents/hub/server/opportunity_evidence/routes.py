"""The routes of the evidence of an opportunity: each one hands the request's parts and the app's engine to the
service, which opens the transaction, checks the caller's role and decides."""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import READ_REFUSALS, WRITE_REFUSALS, ErrorBody
from evo_agents.hub.server.opportunities.models import OpportunityRef
from evo_agents.hub.server.opportunity_evidence import service
from evo_agents.hub.server.opportunity_evidence.models import OpportunityEvidence, OpportunityEvidenceWrite
from evo_agents.hub.server.problem_evidence.models import EvidenceRef
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["opportunities"], responses={401: {"model": ErrorBody}})


@router.get(
    "/{project}/opportunities/{ref}/evidence", response_model=list[OpportunityEvidence], responses=READ_REFUSALS
)
async def list_evidence(
    request: Request, project: ProjectName, ref: OpportunityRef, user: CurrentUser
) -> list[OpportunityEvidence]:
    """The evidence of an opportunity by number, for a member with a grant on the project."""
    return await service.read(request.app.state.engine, user, project, ref)


@router.post(
    "/{project}/opportunities/{ref}/evidence",
    response_model=OpportunityEvidence,
    status_code=201,
    responses=WRITE_REFUSALS,
)
async def add_evidence(
    request: Request, project: ProjectName, ref: OpportunityRef, body: OpportunityEvidenceWrite, user: CurrentUser
) -> OpportunityEvidence:
    """Add a line of evidence to an opportunity; a writer of the project may. 422 for a personal label or an excerpt of
    more than 300 characters, 409 for a line the opportunity has already."""
    state = request.app.state
    return await service.add(state.engine, state.jobs, user, project, ref, body)


@router.post(
    "/{project}/opportunities/{ref}/evidence/{code}/confirm",
    response_model=OpportunityEvidence,
    responses=WRITE_REFUSALS,
)
async def confirm_evidence(
    request: Request, project: ProjectName, ref: OpportunityRef, code: EvidenceRef, user: CurrentUser
) -> OpportunityEvidence:
    """Confirm a line of evidence, once; a writer of the project may."""
    state = request.app.state
    return await service.confirm(state.engine, state.jobs, user, project, ref, code)
