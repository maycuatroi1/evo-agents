"""The two routes of the Curator's ruleset checks (``evo_agents.hub.server.forge.service.protection`` says what they
are): GET /v1/projects/{p}/curator/protection lists a project's repos with the last check of each, and POST
.../curator/protection/{repo}/check checks one now."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.forge.models.protection import Protection, RepoCheck
from evo_agents.hub.server.forge.service import protection as service
from evo_agents.hub.server.runs.models import LINE
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409)}

RepoName = Annotated[str, Path(min_length=1, max_length=200, pattern=LINE)]


@router.get("/{project}/curator/protection", response_model=Protection, responses=REFUSALS)  # pyright: ignore[reportArgumentType]
async def protection(request: Request, project: ProjectName, user: CurrentUser) -> Protection:
    """The project's repos, and whether the hub checked that a ruleset keeps the Curator off each default branch."""
    return await service.protection(request.app.state.engine, user, project)


@router.post(
    "/{project}/curator/protection/{repo}/check",
    response_model=RepoCheck,
    responses={**REFUSALS, 422: {"model": ErrorBody}, 502: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)
async def check_protection(request: Request, project: ProjectName, repo: RepoName, user: CurrentUser) -> RepoCheck:
    """Check now, with the Curator's App, whether a ruleset keeps the Curator off the repo's default branch; the
    project's admins alone may."""
    state = request.app.state
    return await service.check_now(state.engine, getattr(state, "curator_app", None), state.config, user, project, repo)
