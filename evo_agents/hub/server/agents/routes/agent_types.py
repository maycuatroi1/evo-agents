"""The routes of the agent types of plugins in a project: GET /v1/projects/{p}/agent-types, and PUT and DELETE
.../agent-types/{plugin}/{name}; ``evo_agents.hub.server.agents.service.agent_types`` says the rules."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from evo_agents.hub.server.agents.models.agent_types import (
    READ_REFUSALS,
    WRITE_REFUSALS,
    AgentType,
    AgentTypeIn,
    PluginName,
    TypeName,
)
from evo_agents.hub.server.agents.service import agent_types as service
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["agents"], responses={401: {"model": ErrorBody}})


@router.get("/{project}/agent-types", response_model=list[AgentType], responses=READ_REFUSALS)
async def list_agent_types(request: Request, project: ProjectName, user: CurrentUser) -> list[AgentType]:
    """The agent types of the plugins the project enabled, by plugin, then in its manifest's order."""
    async with request.app.state.engine.begin() as conn:
        return await service.list_types(conn, user, project)


@router.put("/{project}/agent-types/{plugin}/{name}", response_model=AgentType, responses=WRITE_REFUSALS)
async def enable_agent_type(
    request: Request, project: ProjectName, plugin: PluginName, name: TypeName, body: AgentTypeIn, user: CurrentUser
) -> AgentType:
    """Enable agent type ``name`` of ``plugin`` in the project, creating its profile the first time, or change how."""
    async with request.app.state.engine.begin() as conn:
        view = await service.enable(conn, request, user, project, plugin, name, body)
    log.info("agent type enabled", extra={"project": project, "plugin": plugin, "agent_type": name})
    return view


@router.delete("/{project}/agent-types/{plugin}/{name}", response_model=AgentType, responses=WRITE_REFUSALS)
async def disable_agent_type(
    request: Request, project: ProjectName, plugin: PluginName, name: TypeName, user: CurrentUser
) -> AgentType:
    """Turn agent type ``name`` of ``plugin`` off in the project: the plugin's tasks are refused; its profile stays."""
    async with request.app.state.engine.begin() as conn:
        view = await service.disable(conn, user, project, plugin, name)
    log.info("agent type turned off", extra={"project": project, "plugin": plugin, "agent_type": name})
    return view
