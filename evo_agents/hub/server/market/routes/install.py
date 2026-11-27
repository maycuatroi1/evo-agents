"""The routes POST /v1/market/{source}/{plugin}/install and /request and POST /v1/plugins/install-github, which install
a plugin and ask for one through ``evo_agents.hub.server.market.service.install``; that module says what each does."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.market.models.install import (
    REFUSALS,
    GitHubInstallIn,
    MarketInstalled,
    MarketInstallIn,
    MarketRequest,
    MarketRequestIn,
)
from evo_agents.hub.server.market.models.market import PluginId, SourceId
from evo_agents.hub.server.market.service import install as service
from evo_agents.hub.server.security import AdminUser, CurrentUser, admin

router = APIRouter(prefix="/v1/market", tags=["market"], responses={401: {"model": ErrorBody}})
github_router = APIRouter(
    prefix="/v1/plugins",
    tags=["plugins"],
    dependencies=[Depends(admin)],
    responses={401: {"model": ErrorBody}, 403: {"model": ErrorBody}},
)


@router.post("/{source}/{plugin}/install", response_model=MarketInstalled, responses=REFUSALS)
async def install_from_market(
    request: Request, source: SourceId, plugin: PluginId, body: MarketInstallIn, user: AdminUser
) -> MarketInstalled:
    """Install a free plugin of a market, or upgrade it, from its GitHub release."""
    return await service.install_from_market(request, source, plugin, body, user)


@github_router.post("/install-github", response_model=MarketInstalled, responses=REFUSALS)
async def install_from_github(request: Request, body: GitHubInstallIn, user: AdminUser) -> MarketInstalled:
    """Install a plugin from the release of a GitHub repo, signed by the signer named (or the repo's ci.yml)."""
    return await service.install_from_github(request, body, user)


@router.post("/{source}/{plugin}/request", response_model=MarketRequest, responses=REFUSALS)
async def request_install(
    request: Request, source: SourceId, plugin: PluginId, body: MarketRequestIn, user: CurrentUser
) -> MarketRequest:
    """Ask the hub admins to install a plugin of a market for project ``project``, of which the caller is an admin."""
    return await service.request_install(request.app.state.engine, request.app.state.config, source, plugin, body, user)
