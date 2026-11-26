"""Reading the findings and proposals of a project, and the owner's answer to a proposal: the routes GET
/v1/projects/{p}/curator/findings, .../findings/{id}, .../proposals, .../proposals/{id} and POST
.../proposals/{id}/answer, which read and answer through ``evo_agents.hub.server.proposals.service.reads``.
``evo_agents.hub.server.proposals.service.record`` says what each does and records what a review run writes."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from pydantic import Field

from evo_agents.hub import review
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.proposals.models import Finding, FindingList, ItemId, ProposalAnswer, ProposalList
from evo_agents.hub.server.proposals.routes.record import READ_REFUSALS, REFUSALS
from evo_agents.hub.server.proposals.service import reads as service
from evo_agents.hub.server.proposals.service.reads import MAX_OFFSET
from evo_agents.hub.server.proposals.service.shapes import Proposal
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

MAX_LIST = 200

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})


@router.get("/{project}/curator/findings", response_model=FindingList, responses=READ_REFUSALS)
async def list_findings(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    run_id: Annotated[int | None, Query(ge=1, le=MAX_ID, description="of this review run")] = None,
    lens: Annotated[review.Lens | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> FindingList:
    """The findings of the project's review runs the caller may read, newest first."""
    return await service.list_findings(request.app.state.engine, user, project, run_id, lens, limit, offset, sink)


@router.get("/{project}/curator/findings/{finding_id}", response_model=Finding, responses=READ_REFUSALS)
async def show_finding(
    request: Request,
    project: ProjectName,
    finding_id: ItemId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> Finding:
    return await service.show_finding(request.app.state.engine, user, project, finding_id, sink)


@router.get("/{project}/curator/proposals", response_model=ProposalList, responses=READ_REFUSALS)
async def list_proposals(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    state: Annotated[list[review.ProposalState], Query(description="any of these; repeat it")] = [],  # noqa: B006
    tier: Annotated[list[Annotated[int, Field(ge=0, le=3)]], Query(description="any of these tiers; repeat it")] = [],  # noqa: B006
    lens: Annotated[review.Lens | None, Query()] = None,
    run_id: Annotated[int | None, Query(ge=1, le=MAX_ID, description="of this review run")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> ProposalList:
    """The proposals of the project's review runs the caller may read, newest first."""
    asked = (state, tier, lens, run_id)
    return await service.list_proposals(request.app.state.engine, user, project, asked, limit, offset, sink)


@router.get("/{project}/curator/proposals/{proposal_id}", response_model=Proposal, responses=READ_REFUSALS)
async def show_proposal(
    request: Request,
    project: ProjectName,
    proposal_id: ItemId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> Proposal:
    return await service.show_proposal(request.app.state.engine, user, project, proposal_id, sink)


@router.post("/{project}/curator/proposals/{proposal_id}/answer", response_model=Proposal, responses=REFUSALS)
async def answer_proposal(
    request: Request, project: ProjectName, proposal_id: ItemId, body: ProposalAnswer, user: CurrentUser
) -> Proposal:
    """Accept, reject or defer a proposal; the admins of the project alone may."""
    answered = await service.answer_proposal(request.app.state.engine, user, project, proposal_id, body)
    log.info("proposal answered", extra={"proposal_id": proposal_id, "action": body.action, "login": user.login})
    return answered
