"""The route GET /v1/projects/{p}/agent-packs, which reads through ``evo_agents.hub.server.agents.service.packs``; that
module says what a profile may attach."""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.agents.models.packs import AgentPack
from evo_agents.hub.server.agents.service import packs as service
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["agents"], responses={401: {"model": ErrorBody}})


@router.get(
    "/{project}/agent-packs",
    response_model=list[AgentPack],
    responses={code: {"model": ErrorBody} for code in (403, 404)},
)
async def list_agent_packs(request: Request, project: ProjectName, user: CurrentUser) -> list[AgentPack]:
    """The agent packs a profile of the project may attach, by plugin, then in its manifest's order."""
    async with request.app.state.engine.begin() as conn:
        return await service.list_packs(conn, user, project, request.app.state.hub_version)
