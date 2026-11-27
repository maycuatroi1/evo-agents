"""The seller's API for buying hubs (plan plugin-market, criterion a7), public under ``/v1/seller-api``
(``security.SELLER_PREFIX``): another hub calls it with no account here, so each route checks itself that the feature
seller is on (404 otherwise) and that the hub can sell (503 without EVO_HUB_SELLER_ID and EVO_HUB_SELLER_KEY).
``evo_agents.hub.seller_client`` is its client.

- GET /v1/seller-api/info: the seller's id and key, and the plugins it lists with their price;
- POST /v1/seller-api/orders: a buying hub orders a listed plugin for a number of months, naming its stable id
  (``hub_`` and 32 hex digits), its address and a contact. The answer is the order's number, ``ORD-YYYY-NNNN``, its
  secret, which this hub keeps only the SHA-256 of, the total, and how to pay it (``evo_agents.hub.payments``). The
  address an order came from (``codes.client_address``), not the hub id it names, has at most MAX_OPEN_PER_BUYER
  orders open, and the seller MAX_OPEN in all, counted before and again under the order lock; an order left open
  ``ORDER_TTL`` (``evo_agents.hub.server.seller.models.seller``) is cancelled;
- GET /v1/seller-api/orders/{number}: with the secret in X-Evo-Order-Secret, the order's state and, once a seller admin
  marked it paid, its licence;
- GET /v1/seller-api/bundles/{plugin}[/{version}]: with ``Authorization: License <token>`` and the hub's id in
  X-Evo-Hub-Id, the signed bundle, in base64 with its signature, for a licence this seller signed, of that exact hub
  and plugin, of a paid order, that covers today or ended less than GRACE_DAYS ago.

Every refusal says why without naming a secret, and the seller logs no secret or token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import features, payments
from evo_agents.hub.seller_client import LICENSE_SCHEME
from evo_agents.hub.server import audit, hub_settings
from evo_agents.hub.server.base.errors import Forbidden, NotFound
from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.plugin_store import fetch_bundle
from evo_agents.hub.server.seller.models.api import (
    MAX_OPEN,
    MAX_OPEN_PER_BUYER,
    UNKNOWN_ORDER,
    OrderIn,
    OrderPlaced,
    OrderState,
    SellerApiInfo,
    SellerBundle,
    SellerListing,
)
from evo_agents.hub.server.seller.models.seller import NOT_SELLING
from evo_agents.hub.server.seller.repository import api as repository
from evo_agents.hub.server.seller.service.keys import own_keys, seller_key, selling
from evo_agents.hub.server.seller.service.orders import expire_orders
from evo_agents.hub.server.workers.service.codes import client_address
from evo_agents.plugins import compat
from evo_agents.plugins import license as lic

log = logging.getLogger(__name__)


async def check_selling(request: Request, conn: AsyncConnection) -> None:
    """404 while the feature seller is off, 503 while the hub cannot sell."""
    instance, _ = await hub_settings.feature_values(conn)
    if not features.enabled(features.BY_ID["seller"], instance):
        raise NotFound("this hub sells no plugins")
    if not selling(request.app.state.config):
        raise HTTPException(503, NOT_SELLING)


def _newest(versions: list[str]) -> str | None:
    return max(versions, key=compat.plugin_version_key) if versions else None


async def info(request: Request) -> SellerApiInfo:
    """The seller's id and key, and what it sells with its newest version."""
    async with request.app.state.engine.begin() as conn:
        await check_selling(request, conn)
        rows, versions = await repository.listed(conn)
    config = request.app.state.config
    found, public = seller_key(config)
    return SellerApiInfo(
        seller=config.seller_id,
        key_id=found,
        public_key=public,
        listings=[
            SellerListing(
                plugin=row.plugin,
                publisher=row.publisher,
                usd_per_hub_month=float(payments.usd(row.usd_per_hub_month)),
                version=_newest(versions.get(row.id, [])),
            )
            for row in rows
        ],
    )


async def _room(conn: AsyncConnection, address: str) -> None:
    """429 when ``address`` or the seller has as many orders open as it may."""
    from_address, in_all = await repository.open_counts(conn, address)
    if from_address >= MAX_OPEN_PER_BUYER:
        raise HTTPException(
            429, f"{MAX_OPEN_PER_BUYER} orders from this address are open here: pay or cancel one before the next"
        )
    if in_all >= MAX_OPEN:
        raise HTTPException(429, "this seller has too many orders open; try again later")


async def place_order(request: Request, body: OrderIn) -> OrderPlaced:
    """Place order ``body`` of a listed plugin from the request's address; its secret is shown once."""
    config = request.app.state.config
    address = client_address(request)
    async with request.app.state.engine.begin() as conn:
        await check_selling(request, conn)
        await expire_orders(conn)
        await _room(conn, address)  # before the order lock: a refused order never waits on it
        listing = await repository.listing_of(conn, body.plugin)
        if listing is None or listing.state != "listed":
            raise NotFound(f"this seller does not sell {body.plugin}")
        number = await repository.next_number(conn, datetime.now(UTC).year)
        await _room(conn, address)  # again under the lock, for the orders placed while this one waited
        per_month = payments.usd(listing.usd_per_hub_month)
        total = payments.usd(per_month * body.months)
        payment = payments.provider(config.seller_payment_instructions).create(
            number, total, {"name": body.contact.name, "email": body.contact.email}
        )
        secret = secrets.token_urlsafe(32)
        await repository.add_order(
            conn,
            number=number,
            listing_id=listing.id,
            buyer_hub=body.hub,
            buyer_url=body.hub_url,
            buyer_address=address,
            buyer_name=body.contact.name,
            buyer_email=body.contact.email,
            months=body.months,
            usd_per_month=per_month,
            usd_total=total,
            secret_hash=hashlib.sha256(secret.encode()).hexdigest(),
            payment=payment.stored(),
        )
        target = f"order:{number} hub:{body.hub} plugin={body.plugin} months={body.months} usd={total}"
        await audit.record(conn, actor_id=None, token_id=None, action=audit.MARKET_ORDER_CREATE, target=target)
    log.info("order placed", extra={"order": number, "plugin": body.plugin})
    return OrderPlaced(
        number=number,
        secret=secret,
        plugin=listing.plugin,
        publisher=listing.publisher,
        months=body.months,
        usd_per_month=float(per_month),
        usd_total=float(total),
        instructions=payment.instructions,
    )


async def order_state(request: Request, number: str, secret: str) -> OrderState:
    """Order ``number``, read with its secret, and its licence once paid; 404 for a wrong secret too."""
    async with request.app.state.engine.begin() as conn:
        await check_selling(request, conn)
        await expire_orders(conn)
        row = await repository.order_with_plugin(conn, number)
    given = hashlib.sha256(secret.encode()).hexdigest()
    if row is None or not hmac.compare_digest(given, row.secret_hash):
        raise NotFound(UNKNOWN_ORDER)
    instructions = None
    if row.state == "open":
        config = request.app.state.config
        instructions = (
            payments.provider(config.seller_payment_instructions)
            .create(row.number, payments.usd(row.usd_total), {})
            .instructions
        )
    return OrderState(
        number=row.number,
        plugin=row.plugin,
        state=row.state,
        months=row.months,
        usd_total=float(payments.usd(row.usd_total)),
        instructions=instructions,
        license=row.token,
        valid_from=None if row.valid_from is None else row.valid_from.isoformat(),
        valid_until=None if row.valid_until is None else row.valid_until.isoformat(),
    )


def _licence(request: Request, authorization: str | None, hub: str, plugin: str) -> lic.License:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme != LICENSE_SCHEME or not token:
        raise HTTPException(401, f"a bundle is served for a licence: Authorization: {LICENSE_SCHEME} <token>")
    try:
        return lic.read_license(token.strip(), own_keys(request.app.state.config), hub=hub, plugin=plugin)
    except lic.LicenseError as exc:
        raise Forbidden(f"refused ({exc.reason}): {exc}") from None


async def bundle(request: Request, plugin: str, version: str | None, authorization: str | None, hub: str):
    """Version ``version`` of ``plugin`` (its newest when None), for a licence of hub ``hub`` that covers today."""
    async with request.app.state.engine.begin() as conn:
        await check_selling(request, conn)
        granted = _licence(request, authorization, hub, plugin)
        order = await repository.order_of_licence(conn, granted.license)
        if order is None or order.state != "paid" or order.buyer_hub != hub:
            raise Forbidden(f"refused (not_ours): licence {granted.license} is not one this seller issued")
        today = datetime.now(UTC).date()
        if not order.valid_from <= today < order.valid_until + timedelta(days=lic.GRACE_DAYS):
            raise Forbidden(
                f"refused (expired): licence {granted.license} covers {order.valid_from} to {order.valid_until}"
            )
        listing = await repository.listing_id(conn, order.listing_id, plugin)
        if listing is None:
            raise NotFound(f"this seller sells no plugin {plugin}")
        rows = await repository.bundles(conn, listing)
    if version is not None:
        row = next((row for row in rows if row.version == version), None)
    else:
        newest = _newest([row.version for row in rows])
        row = next((row for row in rows if row.version == newest), None)
    if row is None:
        raise NotFound(f"this seller has no bundle of {plugin}" + (f" {version}" if version else ""))
    data = await fetch_bundle(blob_store(request), row.sha256, row.size)
    log.info("paid plugin bundle served", extra={"plugin": plugin, "version": row.version, "license": granted.license})
    return SellerBundle(
        plugin=plugin,
        version=row.version,
        sha256=row.sha256,
        size=row.size,
        signature=row.signature,
        bundle=base64.b64encode(data).decode(),
    )
