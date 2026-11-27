"""The routes of the evidence of a problem: each one hands the request's parts and the app's engine to the service,
which opens the transaction, checks the caller's role and decides."""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import READ_REFUSALS, WRITE_REFUSALS, ErrorBody
from evo_agents.hub.server.problem_evidence import service
from evo_agents.hub.server.problem_evidence.models import EvidenceRef, ProblemEvidence, ProblemEvidenceWrite
from evo_agents.hub.server.problems.models import ProblemRef
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["problems"], responses={401: {"model": ErrorBody}})


@router.get("/{project}/problems/{ref}/evidence", response_model=list[ProblemEvidence], responses=READ_REFUSALS)
async def list_evidence(
    request: Request, project: ProjectName, ref: ProblemRef, user: CurrentUser
) -> list[ProblemEvidence]:
    """The evidence of a problem by number, for a member with a grant on the project."""
    return await service.read(request.app.state.engine, user, project, ref)


@router.post(
    "/{project}/problems/{ref}/evidence",
    response_model=ProblemEvidence,
    status_code=201,
    responses=WRITE_REFUSALS,
)
async def add_evidence(
    request: Request, project: ProjectName, ref: ProblemRef, body: ProblemEvidenceWrite, user: CurrentUser
) -> ProblemEvidence:
    """Add a line of evidence to a problem; a writer of the project may. 422 for a personal label or an excerpt of more
    than 300 characters, 409 for a line the problem has already."""
    state = request.app.state
    return await service.add(state.engine, state.jobs, user, project, ref, body)


@router.post(
    "/{project}/problems/{ref}/evidence/{code}/confirm", response_model=ProblemEvidence, responses=WRITE_REFUSALS
)
async def confirm_evidence(
    request: Request, project: ProjectName, ref: ProblemRef, code: EvidenceRef, user: CurrentUser
) -> ProblemEvidence:
    """Confirm a line of evidence, once; a writer of the project may."""
    state = request.app.state
    return await service.confirm(state.engine, state.jobs, user, project, ref, code)
