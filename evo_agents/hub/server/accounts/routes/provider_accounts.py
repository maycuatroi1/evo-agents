"""The routes of a member's provider accounts under /v1/provider-accounts, which read and write through
``evo_agents.hub.server.accounts.service.provider_accounts``; that module says what they do."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from evo_agents.hub.server import gateway_sync
from evo_agents.hub.server.accounts.models.provider_accounts import (
    REFUSALS,
    AccountName,
    ProbeWrite,
    ProviderAccount,
    ProviderAccountWritten,
    ProviderProbe,
    StoredProbe,
)
from evo_agents.hub.server.accounts.models.quota import ProviderQuota
from evo_agents.hub.server.accounts.service import provider_accounts as service
from evo_agents.hub.server.accounts.service.providers import as_probe
from evo_agents.hub.server.accounts.service.shapes import ProviderAccountChange, ProviderAccountWrite
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.autonomy.service import guards as route_autonomy
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/provider-accounts",
    tags=["provider-accounts"],
    dependencies=[route_autonomy.K8],
    responses={401: {"model": ErrorBody}},
)


# Reading


@router.get("", response_model=list[ProviderAccount])
async def list_provider_accounts(request: Request, user: CurrentUser, response: Response) -> list[ProviderAccount]:
    """The caller's provider accounts, by name, without their keys; a hub admin's own too."""
    found = await service.list_accounts(request, user)
    response.headers.update(NO_STORE)
    return found


@router.get("/{name}", response_model=ProviderAccount, responses=REFUSALS)
async def get_provider_account(request: Request, name: AccountName, user: CurrentUser, response: Response):
    """The caller's provider account ``name``, without its key."""
    found = await service.read(request, user, name)
    response.headers.update(NO_STORE)
    return found


# Writing


@router.put("/{name}", response_model=ProviderAccountWritten, responses=REFUSALS)
async def put_provider_account(
    request: Request, name: AccountName, body: ProviderAccountWrite, user: CurrentUser, response: Response
) -> ProviderAccountWritten:
    """Create the caller's provider account ``name``, or replace it whole; the key is sealed and never shown again."""
    written = await service.put(request, user, name, body)
    log.info(
        "provider account written",
        extra={"login": user.login, "provider": body.provider, "replaced": not written.created},
    )
    response.headers.update(NO_STORE)
    gateway_sync.poke(request.app)  # the gateway follows the write
    return written


@router.patch("/{name}", response_model=ProviderAccount, responses=REFUSALS)
async def change_provider_account(
    request: Request, name: AccountName, body: ProviderAccountChange, user: CurrentUser, response: Response
) -> ProviderAccount:
    """Change what ``body`` gives of the caller's account ``name``; the rest, the key included, stays."""
    found = await service.change(request, user, name, body)
    named = sorted(body.model_fields_set)
    log.info("provider account changed", extra={"login": user.login, "fields": [n for n in named if n != "value"]})
    response.headers.update(NO_STORE)
    gateway_sync.poke(request.app)  # the gateway follows the write
    return found


@router.delete("/{name}", status_code=204, response_class=Response, responses=REFUSALS)
async def delete_provider_account(request: Request, name: AccountName, user: CurrentUser) -> Response:
    """Delete the caller's account ``name``: its key and bindings go, its leases and shares are revoked."""
    provider = await service.delete(request, user, name)
    log.info("provider account deleted", extra={"login": user.login, "provider": provider})
    gateway_sync.poke(request.app)  # the gateway follows the write
    return Response(status_code=204)


# Test connection and Fetch models


@router.post("/probe", response_model=ProviderProbe, responses=REFUSALS)
async def probe_endpoint(request: Request, body: ProbeWrite, user: CurrentUser, response: Response) -> ProviderProbe:
    """Call an endpoint not saved yet with the key of ``body``: whether it took it, and the models it lists."""
    found = await service.probe_new(request, user, body)
    response.headers.update(NO_STORE)
    return as_probe(found)


@router.post("/{name}/probe", response_model=ProviderProbe, responses=REFUSALS)
async def probe_provider_account(
    request: Request, name: AccountName, body: StoredProbe, user: CurrentUser, response: Response
) -> ProviderProbe:
    """Call the endpoint of the caller's account ``name`` with its key: whether it took it, and the models it lists."""
    found = await service.probe_stored(request, user, name, body)
    response.headers.update(NO_STORE)
    return as_probe(found)


@router.post("/{name}/quota", response_model=ProviderQuota, responses=REFUSALS)
async def quota_of_provider_account(request: Request, name: AccountName, user: CurrentUser, response: Response):
    """Read the quota the caller's Z.ai account ``name`` has left and keep it as the account's last usage."""
    found = await service.quota(request, user, name)
    response.headers.update(NO_STORE)
    return found
