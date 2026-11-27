"""The routes of the seller under ``/v1/seller`` (``evo_agents.hub.server.seller.service.keys`` says what each does):
they pass the app's engine to the service, which opens the transaction and does the rest."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request

from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import AdminUser, CurrentUser
from evo_agents.hub.server.seller.models.seller import (
    MONTH,
    ORDER_NUMBER,
    REFUSALS,
    BundleIn,
    Listing,
    ListingChange,
    ListingIn,
    ListingPlugin,
    Listings,
    Order,
    OrderPaidIn,
    Orders,
    PayoutIn,
    Revenue,
    SellerInfo,
)
from evo_agents.hub.server.seller.service import keys
from evo_agents.hub.server.seller.service import listings as listing_service
from evo_agents.hub.server.seller.service import orders as order_service
from evo_agents.hub.server.seller.service import revenue as revenue_service

router = APIRouter(prefix="/v1/seller", tags=["seller"], responses={401: {"model": ErrorBody}})


@router.get("", response_model=SellerInfo)
async def seller_info(request: Request, user: CurrentUser) -> SellerInfo:
    """The seller's id and public key, as a market's index lists them, and whether the hub can sell."""
    return keys.seller_info(request.app.state.config)


# Listings


@router.get("/listings", response_model=Listings)
async def listings(request: Request, user: CurrentUser) -> Listings:
    """What this hub sells, with each listing's versions and the licences of it that cover today."""
    return await listing_service.all_listings(request.app.state.engine)


@router.post("/listings", response_model=Listing, status_code=201, responses=REFUSALS)
async def add_listing(request: Request, body: ListingIn, user: CurrentUser) -> Listing:
    """List plugin ``plugin`` of publisher ``publisher`` for sale, owned by the caller, as a draft until its first
    bundle."""
    return await listing_service.add_listing(request.app.state.engine, body, user)


@router.patch("/listings/{plugin}", response_model=Listing, responses=REFUSALS)
async def change_listing(request: Request, plugin: ListingPlugin, body: ListingChange, user: CurrentUser) -> Listing:
    """Change a listing's price (for orders placed from now on), its signer, or list or withdraw it."""
    return await listing_service.change_listing(request.app.state.engine, plugin, body, user)


@router.post("/listings/{plugin}/bundles", response_model=Listing, status_code=201, responses=REFUSALS)
async def upload_bundle(request: Request, plugin: ListingPlugin, body: BundleIn, user: CurrentUser) -> Listing:
    """Upload a signed version of a listed plugin; its first lists the plugin. A version never changes."""
    return await listing_service.upload_bundle(request, plugin, body, user)


# Orders


@router.get("/orders", response_model=Orders)
async def orders(
    request: Request,
    user: AdminUser,
    state: Annotated[Literal["open", "paid", "cancelled"] | None, Query()] = None,
) -> Orders:
    """The orders buying hubs placed, the newest first; ``state`` keeps those in it."""
    return await order_service.orders(request.app.state.engine, state)


@router.post("/orders/{number}/paid", response_model=Order, responses=REFUSALS)
async def mark_paid(request: Request, number: ORDER_NUMBER, body: OrderPaidIn, user: AdminUser) -> Order:
    """Mark an open order paid, with the invoice number, and issue its licence; the buying hub gets it at its next
    check."""
    return await order_service.mark_paid(request, number, body, user)


@router.post("/orders/{number}/cancel", response_model=Order, responses=REFUSALS)
async def cancel_order(request: Request, number: ORDER_NUMBER, user: AdminUser) -> Order:
    """Cancel an open order, which was never paid."""
    return await order_service.cancel_order(request.app.state.engine, number, user)


# Revenue


@router.get("/revenue", response_model=Revenue)
async def revenue(
    request: Request, user: AdminUser, month: Annotated[str | None, Query(pattern=MONTH)] = None
) -> Revenue:
    """The revenue of ``month`` (YYYY-MM, this month in UTC when left out) by publisher, split 80/20."""
    return await revenue_service.month_revenue(request.app.state.engine, month)


@router.post("/payouts", response_model=Revenue, responses=REFUSALS)
async def mark_paid_out(request: Request, body: PayoutIn, user: AdminUser) -> Revenue:
    """Mark a publisher's share of a month paid out; once per publisher and month, after the month is over."""
    revenue_service.check_over(body.month)
    return await revenue_service.mark_paid_out(request.app.state.engine, body, user)
