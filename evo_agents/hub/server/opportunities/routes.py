"""The routes of the opportunities of a project: each one reads its scope (``service.READERS``, ``service.WRITERS``)
and hands the request to the service."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from evo_agents.hub import opportunities
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.base.scope import Scope
from evo_agents.hub.server.errors import READ_REFUSALS, WRITE_REFUSALS, ErrorBody
from evo_agents.hub.server.opportunities import service
from evo_agents.hub.server.opportunities.models import (
    MAX_REVISION,
    OpportunityRef,
    OpportunityRevision,
    ProjectOpportunity,
    ProjectOpportunityClose,
    ProjectOpportunityShown,
    ProjectOpportunityWrite,
    ProjectOpportunityWritten,
)

router = APIRouter(prefix="/v1/projects", tags=["opportunities"], responses={401: {"model": ErrorBody}})

Reading = Annotated[Scope, Depends(service.READERS, scope="function")]
Writing = Annotated[Scope, Depends(service.WRITERS, scope="function")]


@router.get("/{project}/opportunities", response_model=list[ProjectOpportunity], responses=READ_REFUSALS)
async def list_opportunities(
    project: ProjectName,
    scope: Reading,
    status: Annotated[Literal[opportunities.STATUSES] | None, Query(description="those of this status alone")] = None,  # pyright: ignore[reportInvalidTypeForm]
    closing_within: Annotated[
        int | None,
        Query(
            ge=0,
            le=3650,
            description="the open ones whose window ends within this many days (one already past included), "
            "nearest end first; with it, status is open",
        ),
    ] = None,
) -> list[ProjectOpportunity]:
    """The project's opportunities as they are now, by rank, then by code."""
    return await service.listed(scope.conn, scope.user, scope.access, status, closing_within)


@router.get("/{project}/opportunities/{ref}", response_model=ProjectOpportunityShown, responses=READ_REFUSALS)
async def show_opportunity(
    project: ProjectName,
    ref: OpportunityRef,
    scope: Reading,
    revision: Annotated[int | None, Query(ge=1, le=MAX_REVISION, description="an older revision")] = None,
) -> ProjectOpportunityShown:
    """One opportunity of the project, at its newest revision or at ``revision``, with its evidence counted."""
    return await service.shown(scope.conn, scope.user, scope.access, ref, revision)


@router.get(
    "/{project}/opportunities/{ref}/revisions", response_model=list[OpportunityRevision], responses=READ_REFUSALS
)
async def opportunity_revisions(project: ProjectName, ref: OpportunityRef, scope: Reading) -> list[OpportunityRevision]:
    """Every revision of an opportunity, the newest first, each with the fields whose value it changed."""
    return await service.revisions(scope, ref)


@router.put("/{project}/opportunities/{ref}", response_model=ProjectOpportunityWritten, responses=WRITE_REFUSALS)
async def write_opportunity(
    project: ProjectName, ref: OpportunityRef, body: ProjectOpportunityWrite, scope: Writing
) -> ProjectOpportunityWritten:
    """Write an opportunity whole as its next revision, made when the project has none of code ``ref``; a writer of
    the project may."""
    written, changed = await service.put(scope, ref, body)
    return ProjectOpportunityWritten(**written.model_dump(), changed=changed)


@router.post("/{project}/opportunities/{ref}/close", response_model=ProjectOpportunityWritten, responses=WRITE_REFUSALS)
async def close_opportunity(
    project: ProjectName, ref: OpportunityRef, body: ProjectOpportunityClose, scope: Writing
) -> ProjectOpportunityWritten:
    """Close an opportunity as captured, missed or dropped, for a reason, as its next revision; a writer of the
    project may."""
    written, changed = await service.close(scope, ref, body)
    return ProjectOpportunityWritten(**written.model_dump(), changed=changed)
