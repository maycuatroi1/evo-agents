"""The seller's API for buying hubs (plan plugin-market, criterion a7), public under ``/v1/seller-api``
(``security.SELLER_PREFIX``): another hub calls it with no account here, so each route checks itself that the feature
seller is on (404 otherwise) and that the hub can sell (503 without EVO_HUB_SELLER_ID and EVO_HUB_SELLER_KEY).
``evo_agents.hub.seller_client`` is its client.

- GET /v1/seller-api/info: the seller's id and key, and the plugins it lists with their price;
- POST /v1/seller-api/orders: a buying hub orders a listed plugin for a number of months, naming its stable id
  (``hub_`` and 32 hex digits), its address and a contact. The answer is the order's number, ``ORD-YYYY-NNNN``, its
  secret, which this hub keeps only the SHA-256 of, the total, and how to pay it (``evo_agents.hub.payments``). A hub
  has at most MAX_OPEN_PER_HUB orders open, and the seller MAX_OPEN in all;
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
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, func, insert, literal, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import features, payments, tables, tables_seller
from evo_agents.hub.seller_client import HUB_ID_HEADER, LICENSE_SCHEME, ORDER_SECRET_HEADER
from evo_agents.hub.server import audit, hub_settings
from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plugin_store import fetch_bundle
from evo_agents.hub.server.projects import PRINTABLE
from evo_agents.hub.server.seller import NOT_SELLING, own_keys, seller_key, selling
from evo_agents.plugins import compat
from evo_agents.plugins import license as lic
from evo_agents.plugins.manifest import PLUGIN_ID

log = logging.getLogger(__name__)

MAX_OPEN_PER_HUB = 5
MAX_OPEN = 500
ORDER_LOCK = 0x5E11E4  # pg_advisory_xact_lock: one order number at a time
EMAIL = r"^[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+$"
VERSION = r"^[0-9A-Za-z.+-]{1,64}$"
REFUSALS = {code: {"model": ErrorBody} for code in (401, 403, 404, 409, 422, 429, 503)}
UNKNOWN_ORDER = "this seller has no such order, or the secret is not its"

router = APIRouter(prefix="/v1/seller-api", tags=["seller-api"], responses=REFUSALS)


class SellerListing(BaseModel):
    plugin: str
    publisher: str
    usd_per_hub_month: float
    version: str | None = Field(None, description="its newest version for sale")


class SellerApiInfo(BaseModel):
    seller: str
    key_id: str
    public_key: str
    listings: list[SellerListing]


class Contact(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=tables_seller.MAX_CONTACT_CHARS, pattern=PRINTABLE)]
    email: Annotated[str, Field(min_length=3, max_length=tables_seller.MAX_CONTACT_CHARS, pattern=EMAIL)]


class OrderIn(BaseModel):
    plugin: Annotated[str, Field(pattern=PLUGIN_ID)]
    hub: Annotated[str, Field(pattern=f"^{tables_seller.HUB_ID}$", description="the buying hub's stable id")]
    hub_url: Annotated[str | None, Field(max_length=tables_seller.MAX_URL_CHARS, pattern=PRINTABLE)] = None
    months: Annotated[int, Field(ge=1, le=tables_seller.MAX_MONTHS)]
    contact: Contact


class OrderPlaced(BaseModel):
    number: str
    secret: str = Field(description="what the buying hub reads the order with; shown once, kept only hashed here")
    plugin: str
    publisher: str
    months: int
    usd_per_month: float
    usd_total: float
    instructions: str = Field(description="how to pay it")


class OrderState(BaseModel):
    number: str
    plugin: str
    state: str
    months: int
    usd_total: float
    instructions: str | None = Field(None, description="how to pay it, while it is open")
    license: str | None = Field(None, description="the licence's token, once paid")
    valid_from: str | None = None
    valid_until: str | None = None


class SellerBundle(BaseModel):
    plugin: str
    version: str
    sha256: str
    size: int
    signature: dict
    bundle: str = Field(description="the bundle's bytes, in base64")


async def _selling(request: Request, conn: AsyncConnection) -> None:
    """404 while the feature seller is off, 503 while the hub cannot sell."""
    instance, _ = await hub_settings.feature_values(conn)
    if not features.enabled(features.BY_ID["seller"], instance):
        raise HTTPException(404, "this hub sells no plugins")
    if not selling(request.app.state.config):
        raise HTTPException(503, NOT_SELLING)


def _newest(versions: list[str]) -> str | None:
    return max(versions, key=compat.plugin_version_key) if versions else None


@router.get("/info", response_model=SellerApiInfo)
async def info(request: Request) -> SellerApiInfo:
    """The seller's id and key, and what it sells."""
    m, b = tables.market_listings, tables.market_bundles
    async with request.app.state.engine.begin() as conn:
        await _selling(request, conn)
        rows = (await conn.execute(select(m).where(m.c.state == "listed").order_by(m.c.plugin))).all()
        versions: dict[int, list[str]] = {}
        for row in await conn.execute(select(b.c.listing_id, b.c.version)):
            versions.setdefault(row.listing_id, []).append(row.version)
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


async def _number(conn: AsyncConnection, year: int) -> str:
    """The next order number of ``year``, under the order lock."""
    o = tables.market_orders
    await conn.execute(select(func.pg_advisory_xact_lock(literal(ORDER_LOCK, BigInteger))))
    held = await conn.scalar(select(func.count()).where(o.c.number.like(f"ORD-{year}-%")))
    return f"ORD-{year}-{int(held or 0) + 1:04d}"


@router.post("/orders", response_model=OrderPlaced, status_code=201)
async def place_order(request: Request, body: OrderIn) -> OrderPlaced:
    """Order a listed plugin for ``months`` months; pay it as ``instructions`` say, then a seller admin marks it paid."""
    config = request.app.state.config
    m, o = tables.market_listings, tables.market_orders
    async with request.app.state.engine.begin() as conn:
        await _selling(request, conn)
        listing = (await conn.execute(select(m).where(m.c.plugin == body.plugin))).first()
        if listing is None or listing.state != "listed":
            raise HTTPException(404, f"this seller does not sell {body.plugin}")
        number = await _number(conn, datetime.now(UTC).year)
        open_orders = select(func.count()).where(o.c.state == "open")
        if await conn.scalar(open_orders.where(o.c.buyer_hub == body.hub)) >= MAX_OPEN_PER_HUB:
            raise HTTPException(
                429, f"hub {body.hub} has {MAX_OPEN_PER_HUB} orders open here: pay or cancel one before the next"
            )
        if await conn.scalar(open_orders) >= MAX_OPEN:
            raise HTTPException(429, "this seller has too many orders open; try again later")
        per_month = payments.usd(listing.usd_per_hub_month)
        total = payments.usd(per_month * body.months)
        payment = payments.provider(config.seller_payment_instructions).create(
            number, total, {"name": body.contact.name, "email": body.contact.email}
        )
        secret = secrets.token_urlsafe(32)
        await conn.execute(
            insert(o).values(
                number=number,
                listing_id=listing.id,
                buyer_hub=body.hub,
                buyer_url=body.hub_url,
                buyer_name=body.contact.name,
                buyer_email=body.contact.email,
                months=body.months,
                usd_per_month=per_month,
                usd_total=total,
                secret_hash=hashlib.sha256(secret.encode()).hexdigest(),
                payment=payment.stored(),
            )
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


@router.get("/orders/{number}", response_model=OrderState)
async def order_state(
    request: Request,
    number: Annotated[str, Path(pattern=f"^{tables_seller.ORDER_NUMBER}$")],
    secret: Annotated[str, Header(alias=ORDER_SECRET_HEADER, max_length=200)],
) -> OrderState:
    """An order's state, read with its secret, and its licence once paid."""
    o, m = tables.market_orders, tables.market_listings
    async with request.app.state.engine.begin() as conn:
        await _selling(request, conn)
        query = select(o, m.c.plugin).join_from(o, m, m.c.id == o.c.listing_id).where(o.c.number == number)
        row = (await conn.execute(query)).first()
    given = hashlib.sha256(secret.encode()).hexdigest()
    if row is None or not hmac.compare_digest(given, row.secret_hash):
        raise HTTPException(404, UNKNOWN_ORDER)
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
        raise HTTPException(403, f"refused ({exc.reason}): {exc}") from None


async def _bundle(request: Request, plugin: str, version: str | None, authorization: str | None, hub: str):
    o, m, b = tables.market_orders, tables.market_listings, tables.market_bundles
    async with request.app.state.engine.begin() as conn:
        await _selling(request, conn)
        granted = _licence(request, authorization, hub, plugin)
        query = select(o.c.state, o.c.buyer_hub, o.c.valid_from, o.c.valid_until, o.c.listing_id).where(
            o.c.license_id == granted.license
        )
        order = (await conn.execute(query)).first()
        if order is None or order.state != "paid" or order.buyer_hub != hub:
            raise HTTPException(403, f"refused (not_ours): licence {granted.license} is not one this seller issued")
        today = datetime.now(UTC).date()
        if not order.valid_from <= today < order.valid_until + timedelta(days=lic.GRACE_DAYS):
            raise HTTPException(
                403, f"refused (expired): licence {granted.license} covers {order.valid_from} to {order.valid_until}"
            )
        listing = (await conn.execute(select(m.c.id).where(m.c.id == order.listing_id, m.c.plugin == plugin))).first()
        if listing is None:
            raise HTTPException(404, f"this seller sells no plugin {plugin}")
        rows = (await conn.execute(select(b).where(b.c.listing_id == listing.id))).all()
    if version is not None:
        row = next((row for row in rows if row.version == version), None)
    else:
        newest = _newest([row.version for row in rows])
        row = next((row for row in rows if row.version == newest), None)
    if row is None:
        raise HTTPException(404, f"this seller has no bundle of {plugin}" + (f" {version}" if version else ""))
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


HubHeader = Annotated[str, Header(alias=HUB_ID_HEADER, pattern=f"^{tables_seller.HUB_ID}$")]
AuthHeader = Annotated[str | None, Header(alias="Authorization")]
BundlePlugin = Annotated[str, Path(pattern=PLUGIN_ID)]


@router.get("/bundles/{plugin}", response_model=SellerBundle)
async def latest_bundle(request: Request, plugin: BundlePlugin, hub: HubHeader, authorization: AuthHeader = None):
    """The newest version of a plugin, for a licence of the hub that asks."""
    return await _bundle(request, plugin, None, authorization, hub)


@router.get("/bundles/{plugin}/{version}", response_model=SellerBundle)
async def bundle(
    request: Request,
    plugin: BundlePlugin,
    version: Annotated[str, Path(pattern=VERSION)],
    hub: HubHeader,
    authorization: AuthHeader = None,
):
    """A version of a plugin, for a licence of the hub that asks."""
    return await _bundle(request, plugin, version, authorization, hub)
