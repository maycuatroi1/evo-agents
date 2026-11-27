"""The routes of the problems of a project: each one reads its scope (``service.READERS``, ``service.WRITERS``) and
hands the request to the service."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from evo_agents.hub import problems
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.base.scope import Scope
from evo_agents.hub.server.errors import READ_REFUSALS, WRITE_REFUSALS, ErrorBody
from evo_agents.hub.server.problems import service
from evo_agents.hub.server.problems.models import (
    MAX_REVISION,
    ProblemRef,
    ProblemRevision,
    ProjectProblem,
    ProjectProblemClose,
    ProjectProblemShown,
    ProjectProblemWrite,
    ProjectProblemWritten,
)

router = APIRouter(prefix="/v1/projects", tags=["problems"], responses={401: {"model": ErrorBody}})

Reading = Annotated[Scope, Depends(service.READERS, scope="function")]
Writing = Annotated[Scope, Depends(service.WRITERS, scope="function")]


@router.get("/{project}/problems", response_model=list[ProjectProblem], responses=READ_REFUSALS)
async def list_problems(
    project: ProjectName,
    scope: Reading,
    status: Annotated[Literal[problems.STATUSES] | None, Query(description="those of this status alone")] = None,  # pyright: ignore[reportInvalidTypeForm]
) -> list[ProjectProblem]:
    """The project's problems as they are now, by rank, then by code."""
    return await service.listed(scope.conn, scope.access, status)


@router.get("/{project}/problems/{ref}", response_model=ProjectProblemShown, responses=READ_REFUSALS)
async def show_problem(
    project: ProjectName,
    ref: ProblemRef,
    scope: Reading,
    revision: Annotated[int | None, Query(ge=1, le=MAX_REVISION, description="an older revision")] = None,
) -> ProjectProblemShown:
    """One problem of the project, at its newest revision or at ``revision``, with the goals that answer it now."""
    return await service.shown(scope.conn, scope.access, ref, revision)


@router.get("/{project}/problems/{ref}/revisions", response_model=list[ProblemRevision], responses=READ_REFUSALS)
async def problem_revisions(project: ProjectName, ref: ProblemRef, scope: Reading) -> list[ProblemRevision]:
    """Every revision of a problem, the newest first, each with the fields whose value it changed."""
    return await service.revisions(scope, ref)


@router.put("/{project}/problems/{ref}", response_model=ProjectProblemWritten, responses=WRITE_REFUSALS)
async def write_problem(
    project: ProjectName, ref: ProblemRef, body: ProjectProblemWrite, scope: Writing
) -> ProjectProblemWritten:
    """Write a problem whole as its next revision, made when the project has none of code ``ref``; a writer of the
    project may."""
    written, changed = await service.put(scope, ref, body)
    return ProjectProblemWritten(**written.model_dump(), changed=changed)


@router.post("/{project}/problems/{ref}/close", response_model=ProjectProblemWritten, responses=WRITE_REFUSALS)
async def close_problem(
    project: ProjectName, ref: ProblemRef, body: ProjectProblemClose, scope: Writing
) -> ProjectProblemWritten:
    """Close a problem as solved or dropped, for a reason, as its next revision; a writer of the project may."""
    written, changed = await service.close(scope, ref, body)
    return ProjectProblemWritten(**written.model_dump(), changed=changed)
