"""The routes of the intakes of a project: each one reads its scope (``service.READERS``, ``service.WRITERS``) and
hands the request to the service."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from evo_agents.hub import intakes
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.base.scope import Scope
from evo_agents.hub.server.errors import READ_REFUSALS, WRITE_REFUSALS, ErrorBody
from evo_agents.hub.server.intake import service
from evo_agents.hub.server.intake.models import Intake, IntakeRef, IntakeStart

router = APIRouter(prefix="/v1/projects", tags=["intake"], responses={401: {"model": ErrorBody}})

Reading = Annotated[Scope, Depends(service.READERS, scope="function")]
Writing = Annotated[Scope, Depends(service.WRITERS, scope="function")]


@router.get("/{project}/intakes", response_model=list[Intake], responses=READ_REFUSALS)
async def list_intakes(
    project: ProjectName,
    scope: Reading,
    status: Annotated[Literal[intakes.STATUSES] | None, Query(description="keep the intakes of one status")] = None,  # pyright: ignore[reportInvalidTypeForm]
) -> list[Intake]:
    """The project's intakes, newest first, for a member with a grant on the project."""
    return await service.listed(scope, status)


@router.get("/{project}/intakes/{ref}", response_model=Intake, responses=READ_REFUSALS)
async def show_intake(project: ProjectName, ref: IntakeRef, scope: Reading) -> Intake:
    """One intake: its requester, sources, status and, once done, what the hub made of its result."""
    return await service.shown(scope, ref)


@router.post("/{project}/intakes", response_model=Intake, status_code=201, responses=WRITE_REFUSALS)
async def start_intake(project: ProjectName, body: IntakeStart, scope: Writing) -> Intake:
    """Start an intake: a writer of the project asks their session to read sources; it gets the next code I-<n>."""
    return await service.start(scope, body)


@router.put("/{project}/intakes/{ref}", response_model=Intake, responses=WRITE_REFUSALS)
async def put_intake(project: ProjectName, ref: IntakeRef, body: dict, scope: Writing) -> Intake:
    """Put the result of an intake, once, as an evo-intake/1 or evo-intake/2 document (see the module)."""
    return await service.put(scope, ref, body)
