"""The routes of the goals of a project: each one reads its scope (``service.READERS``, ``service.ADMINS``) and hands
the request to the service."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from evo_agents.hub import goals
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.base.scope import Scope
from evo_agents.hub.server.errors import READ_REFUSALS, WRITE_REFUSALS, ErrorBody
from evo_agents.hub.server.goals import service
from evo_agents.hub.server.goals.models import (
    MAX_REVISION,
    GoalRef,
    GoalRevision,
    ProjectGoal,
    ProjectGoalClose,
    ProjectGoalWrite,
    ProjectGoalWritten,
)

router = APIRouter(prefix="/v1/projects", tags=["goals"], responses={401: {"model": ErrorBody}})

Reading = Annotated[Scope, Depends(service.READERS, scope="function")]
Writing = Annotated[Scope, Depends(service.ADMINS, scope="function")]


@router.get("/{project}/goals", response_model=list[ProjectGoal], responses=READ_REFUSALS)
async def list_goals(
    project: ProjectName,
    scope: Reading,
    status: Annotated[Literal[goals.STATUSES] | None, Query(description="those of this status alone")] = None,  # pyright: ignore[reportInvalidTypeForm]
) -> list[ProjectGoal]:
    """The project's goals as they are now, by rank, then by code."""
    return await service.listed(scope, status)


@router.get("/{project}/goals/{ref}", response_model=ProjectGoal, responses=READ_REFUSALS)
async def show_goal(
    project: ProjectName,
    ref: GoalRef,
    scope: Reading,
    revision: Annotated[int | None, Query(ge=1, le=MAX_REVISION, description="an older revision")] = None,
) -> ProjectGoal:
    """One goal of the project, at its newest revision or at ``revision``."""
    return await service.shown(scope, ref, revision)


@router.get("/{project}/goals/{ref}/revisions", response_model=list[GoalRevision], responses=READ_REFUSALS)
async def goal_revisions(project: ProjectName, ref: GoalRef, scope: Reading) -> list[GoalRevision]:
    """Every revision of a goal, the newest first, each with the fields whose value it changed."""
    return await service.revisions(scope, ref)


@router.put("/{project}/goals/{ref}", response_model=ProjectGoalWritten, responses=WRITE_REFUSALS)
async def write_goal(project: ProjectName, ref: GoalRef, body: ProjectGoalWrite, scope: Writing) -> ProjectGoalWritten:
    """Write a goal whole as its next revision, made when the project has none of code ``ref``; an admin of the
    project alone may."""
    shown, changed = await service.put(scope, ref, body)
    return ProjectGoalWritten(**shown.model_dump(), changed=changed)


@router.post("/{project}/goals/{ref}/close", response_model=ProjectGoalWritten, responses=WRITE_REFUSALS)
async def close_goal(project: ProjectName, ref: GoalRef, body: ProjectGoalClose, scope: Writing) -> ProjectGoalWritten:
    """Close a goal for a reason, as its next revision; an admin of the project alone may."""
    shown, changed = await service.close(scope, ref, body)
    return ProjectGoalWritten(**shown.model_dump(), changed=changed)
