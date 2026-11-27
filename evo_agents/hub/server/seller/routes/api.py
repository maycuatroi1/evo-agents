"""The routes of the seller's API for buying hubs under ``/v1/seller-api``
(``evo_agents.hub.server.seller.service.api`` says what each does)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Path, Request

from evo_agents.hub import tables_seller
from evo_agents.hub.seller_client import ORDER_SECRET_HEADER
from evo_agents.hub.server.seller.models.api import (
    REFUSALS,
    VERSION,
    AuthHeader,
    BundlePlugin,
    HubHeader,
    OrderIn,
    OrderPlaced,
    OrderState,
    SellerApiInfo,
    SellerBundle,
)
from evo_agents.hub.server.seller.service import api as service

router = APIRouter(prefix="/v1/seller-api", tags=["seller-api"], responses=REFUSALS)


@router.get("/info", response_model=SellerApiInfo)
async def info(request: Request) -> SellerApiInfo:
    """The seller's id and key, and what it sells."""
    return await service.info(request)


@router.post("/orders", response_model=OrderPlaced, status_code=201)
async def place_order(request: Request, body: OrderIn) -> OrderPlaced:
    """Order a listed plugin for ``months`` months; pay it as ``instructions`` say, then a seller admin marks it paid."""
    return await service.place_order(request, body)


@router.get("/orders/{number}", response_model=OrderState)
async def order_state(
    request: Request,
    number: Annotated[str, Path(pattern=f"^{tables_seller.ORDER_NUMBER}$")],
    secret: Annotated[str, Header(alias=ORDER_SECRET_HEADER, max_length=200)],
) -> OrderState:
    """An order's state, read with its secret, and its licence once paid."""
    return await service.order_state(request, number, secret)


@router.get("/bundles/{plugin}", response_model=SellerBundle)
async def latest_bundle(request: Request, plugin: BundlePlugin, hub: HubHeader, authorization: AuthHeader = None):
    """The newest version of a plugin, for a licence of the hub that asks."""
    return await service.bundle(request, plugin, None, authorization, hub)


@router.get("/bundles/{plugin}/{version}", response_model=SellerBundle)
async def bundle(
    request: Request,
    plugin: BundlePlugin,
    version: Annotated[str, Path(pattern=VERSION)],
    hub: HubHeader,
    authorization: AuthHeader = None,
):
    """A version of a plugin, for a licence of the hub that asks."""
    return await service.bundle(request, plugin, version, authorization, hub)
