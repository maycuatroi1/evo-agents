"""The routes of the decisions on a project's knowledge graph: each one hands the request to the service, which checks
the caller's role."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi import Path as PathParam

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg_decisions import service
from evo_agents.hub.server.kg_decisions.models import (
    KgDecision,
    KgDecisionStep,
    NewKgDecision,
    Number,
    State,
    Step,
)
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(
    prefix="/v1/kg",
    tags=["kg"],
    responses={code: {"model": ErrorBody} for code in (401, 403, 404, 409, 502, 503)},
)


@router.get("/{project}/decisions", response_model=service.KgDecisionList)
async def list_decisions(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    state: Annotated[State | None, Query(description="only the decisions in this state")] = None,
) -> service.KgDecisionList:
    """The decisions on the graph you can see, newest first, with their history and the revision to write against."""
    return await service.listing(request, user, project, state)


@router.post("/{project}/decisions", response_model=KgDecision, status_code=201)
async def take_decision(request: Request, project: ProjectName, body: NewKgDecision, user: CurrentUser) -> KgDecision:
    """Deprecate a node or an edge, merge two node ids or link two nodes, with a reason: active at once."""
    return await service.create(request, user, project, body, propose=False)


@router.post("/{project}/decisions/{number}/{step}", response_model=KgDecision)
async def step_decision(
    request: Request,
    project: ProjectName,
    number: Number,
    step: Annotated[Step, PathParam(description="accept or reject a proposal, or undo an active decision")],
    body: KgDecisionStep,
    user: CurrentUser,
) -> KgDecision:
    """Accept or reject a proposal, or undo an active decision, with a reason."""
    return await service.take_step(request, user, project, number, step, body)
