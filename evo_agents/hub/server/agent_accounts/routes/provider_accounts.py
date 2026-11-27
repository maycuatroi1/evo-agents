"""The routes of the provider accounts of an agent under /v1/projects/{project}/agents/{handle}/provider-accounts,
which read and write through ``evo_agents.hub.server.agent_accounts.service.provider_accounts``; that module says what
each does."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from evo_agents.hub.server.accounts.models.provider_accounts import ProviderAccount, ProviderAccountWritten
from evo_agents.hub.server.accounts.models.quota import ProviderQuota
from evo_agents.hub.server.agent_accounts.models.accounts import AccountName, Handle
from evo_agents.hub.server.agent_accounts.models.provider_accounts import PATH, REFUSALS
from evo_agents.hub.server.agent_accounts.service import provider_accounts as service
from evo_agents.hub.server.agent_accounts.service.shapes import AgentProviderAccountChange, AgentProviderAccountWrite
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.autonomy.service import guards as route_autonomy
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.gateway.service import sync as gateway_sync
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/projects",
    tags=["agents"],
    dependencies=[route_autonomy.K8],
    responses={401: {"model": ErrorBody}},
)


@router.get(PATH, response_model=list[ProviderAccount], responses=REFUSALS)
async def list_agent_provider_accounts(
    request: Request, project: ProjectName, handle: Handle, user: CurrentUser, response: Response
) -> list[ProviderAccount]:
    """The agent's provider accounts, by name, without their keys; any member of the project reads them."""
    found = await service.list_accounts(request, user, project, handle)
    response.headers.update(NO_STORE)
    return found


@router.put(PATH + "/{name}", response_model=ProviderAccountWritten, responses=REFUSALS)
async def put_agent_provider_account(
    request: Request,
    project: ProjectName,
    handle: Handle,
    name: AccountName,
    body: AgentProviderAccountWrite,
    user: CurrentUser,
    response: Response,
) -> ProviderAccountWritten:
    """Give the agent the provider account ``name``, or replace it whole; the key is sealed and never shown again."""
    written = await service.put(request, user, project, handle, name, body)
    log.info("agent provider account written", extra={"project": project, "handle": handle, "created": written.created})
    response.headers.update(NO_STORE)
    gateway_sync.poke(request.app)  # the gateway follows the write
    return written


@router.patch(PATH + "/{name}", response_model=ProviderAccount, responses=REFUSALS)
async def change_agent_provider_account(
    request: Request,
    project: ProjectName,
    handle: Handle,
    name: AccountName,
    body: AgentProviderAccountChange,
    user: CurrentUser,
    response: Response,
) -> ProviderAccount:
    """Change what ``body`` gives of the agent's provider account ``name``; the rest, the key included, stays."""
    found, named = await service.change(request, user, project, handle, name, body)
    log.info("agent provider account changed", extra={"project": project, "handle": handle, "fields": named})
    response.headers.update(NO_STORE)
    gateway_sync.poke(request.app)  # the gateway follows the write
    return found


@router.delete(PATH + "/{name}", status_code=204, response_class=Response, responses=REFUSALS)
async def delete_agent_provider_account(
    request: Request, project: ProjectName, handle: Handle, name: AccountName, user: CurrentUser
) -> Response:
    """Delete the agent's provider account ``name``: its key and bindings go, its leases are revoked."""
    await service.delete(request, user, project, handle, name)
    log.info("agent provider account deleted", extra={"project": project, "handle": handle})
    gateway_sync.poke(request.app)  # the gateway follows the write
    return Response(status_code=204)


@router.post(PATH + "/{name}/quota", response_model=ProviderQuota, responses=REFUSALS)
async def quota_of_agent_provider_account(
    request: Request, project: ProjectName, handle: Handle, name: AccountName, user: CurrentUser, response: Response
) -> ProviderQuota:
    """Read the quota the agent's Z.ai account ``name`` has left and keep it as the account's last usage; any member
    of the project may."""
    found = await service.quota(request, user, project, handle, name)
    response.headers.update(NO_STORE)
    return found
