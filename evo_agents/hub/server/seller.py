"""Selling plugins from this hub (plan plugin-market, criterion a7): the listings, orders, licences, revenue and payouts
of a seller's hub. The feature ``seller`` (``evo_agents.hub.features``), off by default, owns these routes.

- GET /v1/seller: the seller's id, the id and public key of the key that signs its licences (what a market's index
  lists under ``sellers[]``), and whether the hub can sell: EVO_HUB_SELLER_ID and EVO_HUB_SELLER_KEY set;
- GET|POST /v1/seller/listings, PATCH /v1/seller/listings/{plugin}: what the hub sells. Any member signed in with
  GitHub lists a plugin of a publisher, by the signer of its bundles, at a price in USD per hub per month, and owns
  the listing; its owner or a hub admin changes it. A listing is a draft until its first bundle;
- POST /v1/seller/listings/{plugin}/bundles: its owner or a hub admin uploads a signed version, checked against the
  listing's publisher and signer and kept in the blob store; it is never on a public release, and the seller serves it
  only for a licence of the hub that asks (``evo_agents.hub.server.seller_api``);
- GET /v1/seller/orders, POST /v1/seller/orders/{number}/paid and /cancel (hub admins): the orders buying hubs placed;
  marking one paid with the invoice number issues its licence (``evo_agents.plugins.license``), signed with
  EVO_HUB_SELLER_KEY, from the end of the hub's last licence of the plugin when that has not lapsed, else today;
- GET /v1/seller/revenue?month=YYYY-MM and POST /v1/seller/payouts (hub admins): the month's revenue of each
  publisher, the orders paid that month in UTC, split PUBLISHER_PERCENT to the publisher and the rest to evo-agents
  (``evo_agents.hub.payments``), and a publisher's share marked paid out by hand.

Every action on money or a licence is audited (``audit.MARKET_*``); the key and the licence tokens never are, nor are
they logged. With EVO_HUB_SELLER_SIGNAL_PROJECT set the hub writes market.active_licenses and
market.revenue_usd_month to that project, both keys registered there, as each order is paid and daily (the job
hub.market_signals). ``evo-agents hub market orders``, ``paid``, ``revenue`` and ``payout`` are the client.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import secrets
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import payments, signals, tables, tables_seller
from evo_agents.hub.config import HubConfig
from evo_agents.hub.server import audit
from evo_agents.hub.server.blobs import blob_store, put_bytes
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plugin_publishers import SIGNER
from evo_agents.hub.server.projects import PRINTABLE
from evo_agents.hub.server.security import AdminUser, CurrentUser, Principal
from evo_agents.hub.server.signals import SignalIn
from evo_agents.plugins import license as lic
from evo_agents.plugins.bundle import MAX_BUNDLE, BundleError, read_bundle
from evo_agents.plugins.manifest import PLUGIN_ID
from evo_agents.plugins.market import signer_publisher
from evo_agents.plugins.signing import SignatureError, SignatureUnavailable, key_id, verify

log = logging.getLogger(__name__)

NOTHING_UPLOADED = "nothing was uploaded"
NOT_SELLING = (
    "this hub does not sell plugins yet: its operator sets EVO_HUB_SELLER_ID and EVO_HUB_SELLER_KEY (`evo-agents hub "
    "market seller-key` makes the key)"
)
PUBLISHER = r"^[a-z0-9][a-z0-9-]{0,62}$"  # evo_agents.plugins.signing.PUBLISHER_ID
MONTH = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
ORDER_NUMBER = Annotated[str, Path(pattern=f"^{tables_seller.ORDER_NUMBER}$", description="ORD-YYYY-NNNN")]
ListingPlugin = Annotated[str, Path(pattern=PLUGIN_ID, description="the listed plugin's id")]
MAX_PRICE = Decimal("99999999.99")  # market_listings.usd_per_hub_month, numeric(10, 2)
SIGNAL_SOURCE = "market"
SIGNAL_INGEST_KEY = "seller"  # the project's ingest key the seller's signals come through, made on first use
ACTIVE_LICENSES = "market.active_licenses"
REVENUE = "market.revenue_usd_month"
SIGNAL_KEYS = (
    (ACTIVE_LICENSES, "licences", "the licences of this hub's plugins that cover today"),
    (REVENUE, "USD", "the revenue of the orders paid this month, in UTC"),
)
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422, 503)}

router = APIRouter(prefix="/v1/seller", tags=["seller"], responses={401: {"model": ErrorBody}})


# Models


class SellerInfo(BaseModel):
    seller: str | None = Field(description="EVO_HUB_SELLER_ID: the seller's id in the markets that list it")
    key_id: str | None = Field(description="the id of the key that signs the licences, as the market's index lists it")
    public_key: str | None = Field(description="its public key, 32 bytes in base64; the private one is never shown")
    selling: bool = Field(description="whether the hub can sell: the id and the key are both set")
    payment_instructions: str = Field(description="what a buying hub is told to pay an order by")
    signal_project: str | None = Field(description="EVO_HUB_SELLER_SIGNAL_PROJECT")


class ListingBundle(BaseModel):
    version: str
    sha256: str
    size: int
    signed_by: str
    uploaded_by: str | None = None
    uploaded_at: datetime


class Listing(BaseModel):
    plugin: str
    publisher: str
    signer: str
    owner: str | None = None
    usd_per_hub_month: float
    state: Literal["draft", "listed", "withdrawn"]
    bundles: list[ListingBundle] = Field(description="its versions for sale, the newest first")
    active_licenses: int = Field(description="the licences of it that cover today")
    created_at: datetime
    updated_at: datetime


class Listings(BaseModel):
    listings: list[Listing]


class ListingIn(BaseModel):
    plugin: Annotated[str, Field(pattern=PLUGIN_ID)]
    publisher: Annotated[str, Field(pattern=PUBLISHER)]
    signer: Annotated[str, Field(pattern=SIGNER, description="sigstore:OWNER/REPO/.github/workflows/FILE.yml or ...")]
    usd_per_hub_month: Annotated[Decimal, Field(gt=0, le=MAX_PRICE, decimal_places=2)]


class ListingChange(BaseModel):
    usd_per_hub_month: Annotated[Decimal | None, Field(gt=0, le=MAX_PRICE, decimal_places=2)] = None
    signer: Annotated[str | None, Field(pattern=SIGNER)] = None
    state: Literal["listed", "withdrawn"] | None = Field(None, description="listed needs a bundle")


class BundleIn(BaseModel):
    bundle: Annotated[str, Field(min_length=1, max_length=(MAX_BUNDLE * 4) // 3 + 4)] = Field(
        description="the bundle's bytes (tar.gz), in base64"
    )
    signature: dict = Field(description="its signature envelope, as `evo-agents plugin sign` writes it")


class Order(BaseModel):
    number: str
    plugin: str
    publisher: str
    buyer_hub: str
    buyer_url: str | None = None
    buyer_name: str
    buyer_email: str
    months: int
    usd_per_month: float
    usd_total: float
    state: Literal["open", "paid", "cancelled"]
    payment: dict = Field(description="what the payment provider keeps of it: provider and reference")
    created_at: datetime
    invoice: str | None = None
    closed_by: str | None = None
    closed_at: datetime | None = None
    license_id: str | None = None
    valid_from: date | None = None
    valid_until: date | None = Field(None, description="the first day the licence no longer covers")


class Orders(BaseModel):
    orders: list[Order]


class OrderPaidIn(BaseModel):
    invoice: Annotated[str, Field(min_length=1, max_length=tables_seller.MAX_INVOICE_CHARS, pattern=PRINTABLE)]


class Payout(BaseModel):
    publisher_usd: float
    paid_by: str | None = None
    paid_at: datetime
    note: str | None = None


class RevenueLine(BaseModel):
    publisher: str
    orders: int
    gross_usd: float
    publisher_usd: float = Field(description=f"the publisher's {payments.PUBLISHER_PERCENT}%, rounded down to the cent")
    platform_usd: float
    paid_out: Payout | None = Field(None, description="when a hub admin marked the publisher's share paid out")


class Revenue(BaseModel):
    month: str
    publishers: list[RevenueLine]
    gross_usd: float
    publisher_usd: float
    platform_usd: float


class PayoutIn(BaseModel):
    month: Annotated[str, Field(pattern=MONTH)]
    publisher: Annotated[str, Field(pattern=PUBLISHER)]
    note: Annotated[str | None, Field(max_length=tables_seller.MAX_NOTE_CHARS, pattern=PRINTABLE)] = None


# The seller's key


def selling(config: HubConfig) -> bool:
    return bool(config.seller_id and config.seller_key)


def seller_key(config: HubConfig) -> tuple[str, str]:
    """(key id, public key) of the key that signs the licences; 503 when the hub does not sell."""
    if not selling(config):
        raise HTTPException(503, NOT_SELLING)
    public = lic.public_key_of(config.seller_key)
    return key_id(base64.b64decode(public)), public


def own_keys(config: HubConfig) -> dict[str, bytes]:
    """The seller's own key, as ``evo_agents.plugins.license.read_license`` takes the keys of an index."""
    found, public = seller_key(config)
    return {found: base64.b64decode(public)}


@router.get("", response_model=SellerInfo)
async def seller_info(request: Request, user: CurrentUser) -> SellerInfo:
    """The seller's id and public key, as a market's index lists them, and whether the hub can sell."""
    config = request.app.state.config
    found, public = seller_key(config) if selling(config) else (None, None)
    return SellerInfo(
        seller=config.seller_id,
        key_id=found,
        public_key=public,
        selling=selling(config),
        payment_instructions=payments.provider(config.seller_payment_instructions).instructions,
        signal_project=config.seller_signal_project,
    )


# Listings


def _usd(value) -> float:
    return float(payments.usd(value))


def _active(today: date):
    o = tables.market_orders.c
    return and_(o.state == "paid", o.valid_from <= today, o.valid_until > today)


async def listing_row(conn: AsyncConnection, plugin: str, *, lock: bool = False):
    m = tables.market_listings
    query = select(m).where(m.c.plugin == plugin)
    if lock:
        query = query.with_for_update()
    row = (await conn.execute(query)).first()
    if row is None:
        raise HTTPException(
            404, f"this hub lists no plugin {plugin} for sale: see `evo-agents hub market listing list`"
        )
    return row


async def _listings(conn: AsyncConnection, plugin: str | None = None) -> list[Listing]:
    m, b, o, users = tables.market_listings, tables.market_bundles, tables.market_orders, tables.users
    uploader = users.alias("uploader")
    today = datetime.now(UTC).date()
    active = (
        select(func.count()).where(o.c.listing_id == m.c.id, _active(today)).scalar_subquery().label("active_licenses")
    )
    query = (
        select(m, users.c.login.label("owner"), active)
        .join_from(m, users, users.c.id == m.c.owner_id, isouter=True)
        .order_by(m.c.plugin)
    )
    if plugin is not None:
        query = query.where(m.c.plugin == plugin)
    rows = (await conn.execute(query)).all()
    found = select(b, uploader.c.login.label("uploader")).join_from(
        b, uploader, uploader.c.id == b.c.uploaded_by, isouter=True
    )
    bundles: dict[int, list] = {}
    for row in await conn.execute(found.where(b.c.listing_id.in_([row.id for row in rows])).order_by(b.c.id.desc())):
        bundles.setdefault(row.listing_id, []).append(
            ListingBundle(
                version=row.version,
                sha256=row.sha256,
                size=row.size,
                signed_by=row.signed_by,
                uploaded_by=row.uploader,
                uploaded_at=row.uploaded_at,
            )
        )
    return [
        Listing(
            plugin=row.plugin,
            publisher=row.publisher,
            signer=row.signer,
            owner=row.owner,
            usd_per_hub_month=_usd(row.usd_per_hub_month),
            state=row.state,
            bundles=bundles.get(row.id, []),
            active_licenses=row.active_licenses,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


def _may_change(row, user: Principal) -> None:
    if not user.admin and row.owner_id != user.user_id:
        raise HTTPException(403, f"only the member who listed {row.plugin} or a hub admin changes its listing")


async def _audit(conn: AsyncConnection, user: Principal | None, action: str, target: str) -> None:
    await audit.record(
        conn,
        actor_id=None if user is None else user.user_id,
        token_id=None if user is None else user.token_id,
        action=action,
        target=target,
    )


@router.get("/listings", response_model=Listings)
async def listings(request: Request, user: CurrentUser) -> Listings:
    """What this hub sells, with each listing's versions and the licences of it that cover today."""
    async with request.app.state.engine.begin() as conn:
        return Listings(listings=await _listings(conn))


@router.post("/listings", response_model=Listing, status_code=201, responses=REFUSALS)
async def add_listing(request: Request, body: ListingIn, user: CurrentUser) -> Listing:
    """List plugin ``plugin`` of publisher ``publisher`` for sale, owned by the caller, as a draft until its first
    bundle."""
    try:
        signer_publisher(body.publisher, body.signer)
    except ValueError as exc:
        raise HTTPException(422, f"signer {body.signer} does not read: {exc}") from None
    m = tables.market_listings
    async with request.app.state.engine.begin() as conn:
        made = await conn.execute(
            pg_insert(m)
            .values(
                plugin=body.plugin,
                publisher=body.publisher,
                signer=body.signer,
                owner_id=user.user_id,
                usd_per_hub_month=body.usd_per_hub_month,
            )
            .on_conflict_do_nothing()
            .returning(m.c.id)
        )
        if made.first() is None:
            raise HTTPException(409, f"{body.plugin} is listed already: change it with PATCH /v1/seller/listings/...")
        target = f"listing:{body.plugin} usd={body.usd_per_hub_month} state=draft"
        await _audit(conn, user, audit.MARKET_LISTING_ADD, target)
        return (await _listings(conn, body.plugin))[0]


@router.patch("/listings/{plugin}", response_model=Listing, responses=REFUSALS)
async def change_listing(request: Request, plugin: ListingPlugin, body: ListingChange, user: CurrentUser) -> Listing:
    """Change a listing's price (for orders placed from now on), its signer, or list or withdraw it."""
    m, b = tables.market_listings, tables.market_bundles
    async with request.app.state.engine.begin() as conn:
        row = await listing_row(conn, plugin, lock=True)
        _may_change(row, user)
        values = {}
        if body.usd_per_hub_month is not None:
            values["usd_per_hub_month"] = body.usd_per_hub_month
        if body.signer is not None:
            try:
                signer_publisher(row.publisher, body.signer)
            except ValueError as exc:
                raise HTTPException(422, f"signer {body.signer} does not read: {exc}") from None
            values["signer"] = body.signer
        if body.state is not None:
            if body.state == "listed" and not await conn.scalar(select(func.count()).where(b.c.listing_id == row.id)):
                raise HTTPException(409, f"{plugin} has no bundle yet: upload one, which lists it")
            values["state"] = body.state
        if values:
            await conn.execute(update(m).where(m.c.id == row.id).values(**values, updated_at=func.now()))
            price = values.get("usd_per_hub_month", row.usd_per_hub_month)
            target = f"listing:{plugin} usd={price} state={values.get('state', row.state)}"
            await _audit(conn, user, audit.MARKET_LISTING_CHANGE, target)
        return (await _listings(conn, plugin))[0]


def _check_bundle(row, data: bytes, signature: dict, sigstore):
    """The manifest of bundle ``data`` and who signed it: the listing's plugin, by its publisher, isolated, signed by
    its signer; 422 (or 503 when the signature cannot be checked here) otherwise."""
    try:
        manifest = read_bundle(data).manifest
    except BundleError as exc:
        raise HTTPException(422, f"refused (malformed): {exc}; {NOTHING_UPLOADED}") from None
    if manifest.id != row.plugin or manifest.publisher != row.publisher:
        raise HTTPException(
            422,
            f"the bundle is {manifest.id} of {manifest.publisher}, not {row.plugin} of {row.publisher}; "
            f"{NOTHING_UPLOADED}",
        )
    if manifest.tier != "isolated":
        raise HTTPException(422, f"a paid plugin is isolated: {manifest.id} is a core plugin; {NOTHING_UPLOADED}")
    try:
        signed = verify(data, signature, row.publisher, (signer_publisher(row.publisher, row.signer),), sigstore)
    except SignatureError as exc:
        raise HTTPException(422, f"refused ({exc.reason}): {exc}; {NOTHING_UPLOADED}") from None
    except SignatureUnavailable as exc:
        raise HTTPException(
            503, f"the signature cannot be checked on this hub now: {exc}; {NOTHING_UPLOADED}"
        ) from None
    return manifest, signed


@router.post("/listings/{plugin}/bundles", response_model=Listing, status_code=201, responses=REFUSALS)
async def upload_bundle(request: Request, plugin: ListingPlugin, body: BundleIn, user: CurrentUser) -> Listing:
    """Upload a signed version of a listed plugin; its first lists the plugin. A version never changes."""
    state = request.app.state
    try:
        data = base64.b64decode(body.bundle, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, f"bundle is the bundle's bytes in base64; {NOTHING_UPLOADED}") from None
    if len(data) > MAX_BUNDLE:
        raise HTTPException(422, f"a plugin bundle is at most {MAX_BUNDLE // (1024 * 1024)} MiB; {NOTHING_UPLOADED}")
    async with state.engine.begin() as conn:
        row = await listing_row(conn, plugin)
        _may_change(row, user)
    manifest, signed = await asyncio.to_thread(_check_bundle, row, data, body.signature, state.plugin_sigstore)
    sha256 = hashlib.sha256(data).hexdigest()
    await asyncio.to_thread(put_bytes, blob_store(request), sha256, data)
    m, b, blobs = tables.market_listings, tables.market_bundles, tables.blobs
    async with state.engine.begin() as conn:
        row = await listing_row(conn, plugin, lock=True)
        same = (
            await conn.execute(select(b.c.sha256).where(b.c.listing_id == row.id, b.c.version == manifest.version))
        ).first()
        if same is not None:
            if same.sha256 != sha256:
                raise HTTPException(
                    409,
                    f"version {manifest.version} of {plugin} is uploaded with another bundle; a version never changes, "
                    "upload the change as a new version",
                )
            return (await _listings(conn, plugin))[0]
        await conn.execute(
            pg_insert(blobs)
            .values(project_id=None, sha256=sha256, size=len(data), kind="plugin-bundle", created_by=user.user_id)
            .on_conflict_do_nothing()
        )
        await conn.execute(
            insert(b).values(
                listing_id=row.id,
                version=manifest.version,
                sha256=sha256,
                size=len(data),
                signature=body.signature,
                signed_by=signed.signed_by,
                uploaded_by=user.user_id,
            )
        )
        if row.state == "draft":
            await conn.execute(update(m).where(m.c.id == row.id).values(state="listed", updated_at=func.now()))
        await _audit(conn, user, audit.MARKET_BUNDLE_UPLOAD, f"listing:{plugin}@{manifest.version}")
        result = (await _listings(conn, plugin))[0]
    log.info("paid plugin bundle uploaded", extra={"plugin": plugin, "version": manifest.version})
    return result


# Orders


def _orders_query():
    o, m, users = tables.market_orders, tables.market_listings, tables.users
    return (
        select(o, m.c.plugin, m.c.publisher, users.c.login.label("closer"))
        .join_from(o, m, m.c.id == o.c.listing_id)
        .join_from(o, users, users.c.id == o.c.closed_by, isouter=True)
    )


def order_model(row) -> Order:
    payment = {key: value for key, value in (row.payment or {}).items() if key in ("provider", "reference")}
    return Order(
        number=row.number,
        plugin=row.plugin,
        publisher=row.publisher,
        buyer_hub=row.buyer_hub,
        buyer_url=row.buyer_url,
        buyer_name=row.buyer_name,
        buyer_email=row.buyer_email,
        months=row.months,
        usd_per_month=_usd(row.usd_per_month),
        usd_total=_usd(row.usd_total),
        state=row.state,
        payment=payment,
        created_at=row.created_at,
        invoice=row.invoice,
        closed_by=row.closer,
        closed_at=row.closed_at,
        license_id=row.license_id,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
    )


async def order_row(conn: AsyncConnection, number: str, *, lock: bool = False):
    query = _orders_query().where(tables.market_orders.c.number == number)
    if lock:
        query = query.with_for_update(of=tables.market_orders)
    row = (await conn.execute(query)).first()
    if row is None:
        raise HTTPException(404, f"this hub has no order {number}: see `evo-agents hub market orders`")
    return row


@router.get("/orders", response_model=Orders)
async def orders(
    request: Request,
    user: AdminUser,
    state: Annotated[Literal["open", "paid", "cancelled"] | None, Query()] = None,
) -> Orders:
    """The orders buying hubs placed, the newest first; ``state`` keeps those in it."""
    o = tables.market_orders
    query = _orders_query().order_by(o.c.id.desc())
    if state is not None:
        query = query.where(o.c.state == state)
    async with request.app.state.engine.begin() as conn:
        return Orders(orders=[order_model(row) for row in await conn.execute(query)])


async def licence_start(conn: AsyncConnection, row, today: date) -> date:
    """Where the licence of order ``row`` starts: the end of the buying hub's last licence of the plugin when that has
    not lapsed (a renewal runs on from it), else today."""
    o = tables.market_orders
    last = await conn.scalar(
        select(func.max(o.c.valid_until)).where(
            o.c.buyer_hub == row.buyer_hub, o.c.listing_id == row.listing_id, o.c.state == "paid", o.c.id != row.id
        )
    )
    if last is not None and today < last + timedelta(days=lic.GRACE_DAYS):
        return last
    return today


def sign(config: HubConfig, row, start: date, issued: datetime) -> tuple[lic.License, str]:
    """The licence of paid order ``row`` from ``start``, and its token, signed with EVO_HUB_SELLER_KEY."""
    found, _ = seller_key(config)
    granted = lic.License(
        license=f"lic_{secrets.token_hex(8)}",
        plugin=row.plugin,
        publisher=row.publisher,
        seller=config.seller_id,
        hub=row.buyer_hub,
        order=row.number,
        key=found,
        valid_from=start,
        valid_until=lic.add_months(start, row.months),
        issued=issued.isoformat(timespec="seconds"),
    )
    return granted, lic.sign_license(granted, config.seller_key)


@router.post("/orders/{number}/paid", response_model=Order, responses=REFUSALS)
async def mark_paid(request: Request, number: ORDER_NUMBER, body: OrderPaidIn, user: AdminUser) -> Order:
    """Mark an open order paid, with the invoice number, and issue its licence; the buying hub gets it at its next
    check."""
    config = request.app.state.config
    seller_key(config)
    o = tables.market_orders
    async with request.app.state.engine.begin() as conn:
        row = await order_row(conn, number, lock=True)
        if row.state != "open":
            raise HTTPException(409, f"order {number} is {row.state}, not open")
        now = datetime.now(UTC)
        start = await licence_start(conn, row, now.date())
        granted, token = sign(config, row, start, now)
        await conn.execute(
            update(o)
            .where(o.c.id == row.id)
            .values(
                state="paid",
                invoice=body.invoice,
                closed_by=user.user_id,
                closed_at=func.now(),
                license_id=granted.license,
                valid_from=granted.valid_from,
                valid_until=granted.valid_until,
                key_id=granted.key,
                token=token,
                issued_at=now,
            )
        )
        await _audit(conn, user, audit.MARKET_ORDER_PAID, f"order:{number} hub:{row.buyer_hub} invoice={body.invoice}")
        target = (
            f"license:{granted.license} order:{number} hub:{row.buyer_hub} plugin={row.plugin} "
            f"until={granted.valid_until.isoformat()}"
        )
        await _audit(conn, user, audit.MARKET_LICENSE_ISSUE, target)
        result = order_model(await order_row(conn, number))
    log.info("order paid, licence issued", extra={"order": number, "license": granted.license})
    await push_signals_logged(request.app.state.engine, config)
    return result


@router.post("/orders/{number}/cancel", response_model=Order, responses=REFUSALS)
async def cancel_order(request: Request, number: ORDER_NUMBER, user: AdminUser) -> Order:
    """Cancel an open order, which was never paid."""
    o = tables.market_orders
    async with request.app.state.engine.begin() as conn:
        row = await order_row(conn, number, lock=True)
        if row.state != "open":
            raise HTTPException(409, f"order {number} is {row.state}, not open")
        await conn.execute(
            update(o).where(o.c.id == row.id).values(state="cancelled", closed_by=user.user_id, closed_at=func.now())
        )
        await _audit(conn, user, audit.MARKET_ORDER_CANCEL, f"order:{number} hub:{row.buyer_hub}")
        return order_model(await order_row(conn, number))


# Revenue


def month_bounds(month: str) -> tuple[datetime, datetime]:
    year, number = (int(part) for part in month.split("-"))
    start = datetime(year, number, 1, tzinfo=UTC)
    end = datetime(year + (number == 12), number % 12 + 1, 1, tzinfo=UTC)
    return start, end


async def revenue_of(conn: AsyncConnection, month: str) -> Revenue:
    """The revenue of ``month``: the orders paid in it (UTC), by publisher, split, with what was paid out."""
    o, m, p, users = tables.market_orders, tables.market_listings, tables.market_payouts, tables.users
    start, end = month_bounds(month)
    query = (
        select(m.c.publisher, func.count().label("orders"), func.sum(o.c.usd_total).label("gross"))
        .join_from(o, m, m.c.id == o.c.listing_id)
        .where(o.c.state == "paid", o.c.closed_at >= start, o.c.closed_at < end)
        .group_by(m.c.publisher)
        .order_by(m.c.publisher)
    )
    paid = (
        select(p, users.c.login)
        .join_from(p, users, users.c.id == p.c.paid_by, isouter=True)
        .where(p.c.month == start.date())
    )
    outs = {row.publisher: row for row in await conn.execute(paid)}
    lines, totals = [], [Decimal(0), Decimal(0), Decimal(0)]
    for row in await conn.execute(query):
        gross = payments.usd(row.gross)
        share, platform = payments.split(gross)
        out = outs.get(row.publisher)
        lines.append(
            RevenueLine(
                publisher=row.publisher,
                orders=row.orders,
                gross_usd=float(gross),
                publisher_usd=float(share),
                platform_usd=float(platform),
                paid_out=None
                if out is None
                else Payout(
                    publisher_usd=_usd(out.publisher_usd), paid_by=out.login, paid_at=out.paid_at, note=out.note
                ),
            )
        )
        totals = [totals[0] + gross, totals[1] + share, totals[2] + platform]
    return Revenue(
        month=month,
        publishers=lines,
        gross_usd=float(totals[0]),
        publisher_usd=float(totals[1]),
        platform_usd=float(totals[2]),
    )


def this_month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


@router.get("/revenue", response_model=Revenue)
async def revenue(
    request: Request, user: AdminUser, month: Annotated[str | None, Query(pattern=MONTH)] = None
) -> Revenue:
    """The revenue of ``month`` (YYYY-MM, this month in UTC when left out) by publisher, split 80/20."""
    async with request.app.state.engine.begin() as conn:
        return await revenue_of(conn, month or this_month())


@router.post("/payouts", response_model=Revenue, responses=REFUSALS)
async def mark_paid_out(request: Request, body: PayoutIn, user: AdminUser) -> Revenue:
    """Mark a publisher's share of a month paid out; once per publisher and month, after the month is over."""
    start, end = month_bounds(body.month)
    if end > datetime.now(UTC):
        raise HTTPException(409, f"{body.month} is not over yet: its revenue can still grow")
    p = tables.market_payouts
    async with request.app.state.engine.begin() as conn:
        found = await revenue_of(conn, body.month)
        line = next((line for line in found.publishers if line.publisher == body.publisher), None)
        if line is None:
            raise HTTPException(404, f"publisher {body.publisher} has no revenue in {body.month}")
        if line.paid_out is not None:
            raise HTTPException(409, f"the share of {body.publisher} for {body.month} is marked paid out already")
        gross = payments.usd(line.gross_usd)
        share, platform = payments.split(gross)
        await conn.execute(
            insert(p).values(
                month=start.date(),
                publisher=body.publisher,
                gross_usd=gross,
                publisher_usd=share,
                platform_usd=platform,
                paid_by=user.user_id,
                note=body.note,
            )
        )
        await _audit(conn, user, audit.MARKET_PAYOUT, f"payout:{body.month} publisher:{body.publisher} usd={share}")
        return await revenue_of(conn, body.month)


# Signals


async def _signal_pusher(conn: AsyncConnection, project_id: int) -> signals.Pusher | None:
    """The pusher of the project's ingest key SIGNAL_INGEST_KEY, made on first use and owned by the project's first
    admin, with both keys registered; None when the project has no admin or revoked the key."""
    k, g, sk = tables.signal_ingest_keys, tables.grants, tables.signal_keys
    owner = await conn.scalar(
        select(g.c.user_id).where(g.c.project_id == project_id, g.c.role == "admin").order_by(g.c.user_id).limit(1)
    )
    if owner is None:
        return None
    for name, unit, description in SIGNAL_KEYS:
        await conn.execute(
            pg_insert(sk)
            .values(
                project_id=project_id,
                name=name,
                kind="number",
                unit=unit,
                description=description,
                source=SIGNAL_SOURCE,
                created_by=owner,
            )
            .on_conflict_do_nothing()
        )
    rules = await signals.project_rules(conn, project_id)
    found = (
        await conn.execute(
            select(k.c.id, k.c.max_level, k.c.revoked_at).where(
                k.c.project_id == project_id, k.c.name == SIGNAL_INGEST_KEY
            )
        )
    ).all()
    live = next((row for row in found if row.revoked_at is None), None)
    if live is None and found:
        return None
    if live is None:
        made = await conn.execute(
            insert(k)
            .values(
                project_id=project_id,
                name=SIGNAL_INGEST_KEY,
                max_level=rules.default_label.get("level"),
                token_hash=signals.hash_token(signals.new_ingest_token()),  # nobody reads it: the hub writes itself
                created_by=owner,
            )
            .returning(k.c.id, k.c.max_level)
        )
        live = made.one()
    return signals.Pusher(project_id, rules, "ingest", live.id, SIGNAL_INGEST_KEY, live.max_level)


async def sales_figures(conn: AsyncConnection, today: date) -> tuple[int, Decimal]:
    """The licences that cover ``today`` and the revenue of its month so far."""
    o = tables.market_orders
    active = await conn.scalar(select(func.count()).where(_active(today)))
    start, end = month_bounds(today.strftime("%Y-%m"))
    total = await conn.scalar(
        select(func.coalesce(func.sum(o.c.usd_total), 0)).where(
            o.c.state == "paid", o.c.closed_at >= start, o.c.closed_at < end
        )
    )
    return int(active or 0), payments.usd(total)


async def push_signals(engine: AsyncEngine, config: HubConfig) -> dict:
    """Write market.active_licenses and market.revenue_usd_month to EVO_HUB_SELLER_SIGNAL_PROJECT; what was done."""
    project = config.seller_signal_project
    if not project:
        return {"pushed": 0, "reason": "no EVO_HUB_SELLER_SIGNAL_PROJECT"}
    p = tables.projects
    async with engine.begin() as conn:
        project_id = await conn.scalar(select(p.c.id).where(p.c.name == project))
        if project_id is None:
            return {"pushed": 0, "reason": f"no project {project}"}
        pusher = await _signal_pusher(conn, project_id)
        if pusher is None:
            return {"pushed": 0, "reason": f"project {project} has no admin, or revoked the key {SIGNAL_INGEST_KEY}"}
        active, total = await sales_figures(conn, datetime.now(UTC).date())
        items = [
            SignalIn(key=ACTIVE_LICENSES, value=active, source=SIGNAL_SOURCE),
            SignalIn(key=REVENUE, value=float(total), source=SIGNAL_SOURCE),
        ]
        done = await signals.push(conn, pusher, items, config.signal_days)
    return {"pushed": done.accepted, "active_licenses": active, "revenue_usd_month": float(total)}


async def push_signals_logged(engine: AsyncEngine, config: HubConfig) -> None:
    """``push_signals``, whose failure the hub logs and an order's payment does not wait on."""
    if not config.seller_signal_project:
        return
    try:
        await push_signals(engine, config)
    except Exception:  # the order is paid either way; the daily job writes them again
        log.warning("the seller's signals could not be written", exc_info=True)
