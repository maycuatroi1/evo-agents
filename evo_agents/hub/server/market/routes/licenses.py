"""The routes GET /v1/market/hub, POST /v1/market/{source}/{plugin}/buy, GET /v1/market/licenses and POST
/v1/market/licenses/check, which buy a paid plugin and read its licences through
``evo_agents.hub.server.market.service.licenses``; that module says what each does."""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.market.models.licenses import REFUSALS, Bought, BuyIn, Checked, HubIdentity, Purchases
from evo_agents.hub.server.market.models.market import PluginId, SourceId
from evo_agents.hub.server.market.service import licenses as service
from evo_agents.hub.server.security import AdminUser

router = APIRouter(prefix="/v1/market", tags=["market"], responses={401: {"model": ErrorBody}})


@router.get("/hub", response_model=HubIdentity)
async def hub_identity(request: Request, user: AdminUser) -> HubIdentity:
    """The hub's stable id, which a seller's licence names."""
    async with request.app.state.engine.begin() as conn:
        return await service.hub_identity(conn, request.app.state.config.public_url)


@router.post("/{source}/{plugin}/buy", response_model=Bought, status_code=201, responses=REFUSALS)
async def buy(request: Request, source: SourceId, plugin: PluginId, body: BuyIn, user: AdminUser) -> Bought:
    """Order a paid plugin of a market from its seller; the hub shows the order's number and how to pay it."""
    return await service.buy(request, source, plugin, body, user)


@router.get("/licenses", response_model=Purchases)
async def purchases(request: Request, user: AdminUser) -> Purchases:
    """The hub's orders of paid plugins and their licences, the newest first."""
    async with request.app.state.engine.begin() as conn:
        return await service.purchases(conn)


@router.post("/licenses/check", response_model=Checked, responses=REFUSALS)
async def check_now(request: Request, user: AdminUser) -> Checked:
    """Ask the sellers about the open orders now, install what a licence covers, and turn off what lapsed."""
    return await service.check_now(request)
