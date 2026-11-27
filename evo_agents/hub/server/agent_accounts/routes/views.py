"""The routes GET /v1/projects/{p}/agents/{handle}/accounts, GET .../accounts/{name} and GET /v1/agent-accounts,
which read through ``evo_agents.hub.server.agent_accounts.service.views``; that module says what each reads."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from evo_agents.hub import profiles
from evo_agents.hub.server.agent_accounts.models.accounts import AccountName, Handle
from evo_agents.hub.server.agent_accounts.models.views import REFUSALS
from evo_agents.hub.server.agent_accounts.service import views as service
from evo_agents.hub.server.agent_accounts.service.shapes import AgentAccount, ListedAgentAccount
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1", tags=["agents"], responses={401: {"model": ErrorBody}})


@router.get("/projects/{project}/agents/{handle}/accounts", response_model=list[AgentAccount], responses=REFUSALS)
async def list_agent_accounts(
    request: Request, project: ProjectName, handle: Handle, user: CurrentUser, response: Response
) -> list[AgentAccount]:
    """The live accounts of agent ``handle``, by variable and name, with their figures; never a value."""
    async with request.app.state.engine.begin() as conn:
        found = await service.list_accounts(conn, user, project, handle)
    response.headers.update(NO_STORE)
    return found


@router.get("/projects/{project}/agents/{handle}/accounts/{name}", response_model=AgentAccount, responses=REFUSALS)
async def get_agent_account(
    request: Request, project: ProjectName, handle: Handle, name: AccountName, user: CurrentUser, response: Response
) -> AgentAccount:
    """The account ``name`` of agent ``handle`` with its figures; 404 when the agent has no live account of that name."""
    async with request.app.state.engine.begin() as conn:
        found = await service.show(conn, user, project, handle, name)
    response.headers.update(NO_STORE)
    return found


@router.get("/agent-accounts", response_model=list[ListedAgentAccount])
async def list_all_agent_accounts(
    request: Request,
    user: CurrentUser,
    response: Response,
    project: Annotated[str | None, Query(description="only the agents of this project")] = None,
    handle: Annotated[
        str | None, Query(pattern=profiles.HANDLE_PATTERN, description="only the agent of this handle")
    ] = None,
) -> list[ListedAgentAccount]:
    """The live accounts of the agents of every project the caller holds a grant on, by project, handle, variable and
    name; a hub admin's grants alone count, as for reading a profile."""
    async with request.app.state.engine.begin() as conn:
        found = await service.list_all(conn, user, project, handle)
    response.headers.update(NO_STORE)
    return found
