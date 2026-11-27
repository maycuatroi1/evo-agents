"""The routes PUT, PATCH and DELETE /v1/projects/{p}/agents/{handle}/accounts/{name}, which write the accounts of an
agent through ``evo_agents.hub.server.agent_accounts.service.accounts``; that module says what each does."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from evo_agents.hub.server.agent_accounts.models.accounts import REFUSALS, AccountName, Handle
from evo_agents.hub.server.agent_accounts.service import accounts as service
from evo_agents.hub.server.agent_accounts.service.shapes import (
    AgentAccount,
    AgentAccountChange,
    AgentAccountWrite,
    AgentAccountWritten,
)
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["agents"], responses={401: {"model": ErrorBody}})


@router.put("/{project}/agents/{handle}/accounts/{name}", response_model=AgentAccountWritten, responses=REFUSALS)
async def put_agent_account(
    request: Request,
    project: ProjectName,
    handle: Handle,
    name: AccountName,
    body: AgentAccountWrite,
    user: CurrentUser,
    response: Response,
) -> AgentAccountWritten:
    """Give the agent the account ``name``, or replace it whole; the value is sealed and never shown again."""
    async with request.app.state.engine.begin() as conn:
        written = await service.put(conn, request, user, project, handle, name, body)
    log.info(
        "agent account written",
        extra={"project": project, "handle": handle, "login": user.login, "was_created": written.created},
    )
    response.headers.update(NO_STORE)
    return written


@router.patch("/{project}/agents/{handle}/accounts/{name}", response_model=AgentAccount, responses=REFUSALS)
async def change_agent_account(
    request: Request,
    project: ProjectName,
    handle: Handle,
    name: AccountName,
    body: AgentAccountChange,
    user: CurrentUser,
    response: Response,
) -> AgentAccount:
    """Renew the account (``value``), or change its weight, priority, workers or end, or pause or resume it."""
    async with request.app.state.engine.begin() as conn:
        changed, named = await service.change(conn, request, user, project, handle, name, body)
    log.info(
        "agent account changed", extra={"project": project, "handle": handle, "login": user.login, "fields": named}
    )
    response.headers.update(NO_STORE)
    return changed


@router.delete(
    "/{project}/agents/{handle}/accounts/{name}", status_code=204, response_class=Response, responses=REFUSALS
)
async def delete_agent_account(
    request: Request, project: ProjectName, handle: Handle, name: AccountName, user: CurrentUser
) -> Response:
    """Delete the account: its value and bindings go, and its leases not given back are revoked."""
    async with request.app.state.engine.begin() as conn:
        revoked = await service.delete(conn, user, project, handle, name)
    log.info(
        "agent account deleted",
        extra={"project": project, "handle": handle, "login": user.login, "leases_revoked": revoked},
    )
    return Response(status_code=204)
