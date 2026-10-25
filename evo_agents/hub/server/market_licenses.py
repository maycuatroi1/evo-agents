"""Buying a paid plugin from its seller (plan plugin-market, criterion a7): the buyer's side of
``evo_agents.hub.server.seller``.

- GET /v1/market/hub: the hub's stable id, ``hub_`` and 32 hex digits, made the first time it is asked for
  (``hub_identity``); a seller's licence names it, and the seller serves the bundle to that hub alone;
- POST /v1/market/{source}/{plugin}/buy (hub admins): order a paid plugin of a market for ``months`` months from the
  seller the market lists for it, with a contact. The order carries the hub's id and address; the seller answers its
  number, the total and how to pay it (a bank transfer), which the hub shows. The order's secret is sealed with the
  hub's key (EVO_HUB_SECRETS_KEY, 503 without one). A signer the hub does not trust for the publisher yet is a 409
  unless the admin confirms it (``trust_publisher``), as an install from a market is;
- GET /v1/market/licenses (hub admins): the hub's orders and licences, never their secrets or tokens;
- POST /v1/market/licenses/check (hub admins): check now what the job hub.market_licenses checks every hour.

``check_licenses`` asks each seller about the hub's open orders. A paid one carries its licence, which the hub checks
offline against the keys the market's index lists for the seller, for this hub and plugin, and keeps. A plugin a
licence covers today is then downloaded from the seller with that licence, checked as any bundle is, and installed as
the admin who ordered it; one turned off when its licence lapsed is turned on again. REMIND_DAYS before the last
licence of an installed plugin ends, the hub admins get a notice ``market_license_expiring``; GRACE_DAYS after it ends
the plugin is turned off on the whole hub, its config kept (``plugins.disabled_reason`` starting LAPSED), and a
notice ``plugin_disabled`` says so; a renewal turns it on again. ``POST /v1/plugins/{name}/install`` refuses a bought
plugin no licence covers. Every licence received or lapsed is audited.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import tables, tables_seller
from evo_agents.hub.seller_client import SellerClient, SellerUnavailable
from evo_agents.hub.server import audit
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.market import PluginId, SourceId, entry_of, source_row, source_rows
from evo_agents.hub.server.market_install import Installing, check_trust, install_checked
from evo_agents.hub.server.notifications import notify
from evo_agents.hub.server.plugin_publishers import trust
from evo_agents.hub.server.plugin_store import (
    ADMIN_PAGE,
    MAX_REASON_CHARS,
    find,
    hub_admin_ids,
    set_installed,
)
from evo_agents.hub.server.plugin_store import NOTICE as DISABLED_NOTICE
from evo_agents.hub.server.plugin_tokens import revoke_tokens
from evo_agents.hub.server.sealing import Sealer, Unsealable, market_purchase_aad
from evo_agents.hub.server.security import AdminUser
from evo_agents.hub.server.seller_api import Contact
from evo_agents.plugins import license as lic
from evo_agents.plugins.bundle import BundleError, read_bundle
from evo_agents.plugins.market import CheckedRelease, signer_publisher
from evo_agents.plugins.signing import SignatureError, SignatureUnavailable, verify

log = logging.getLogger(__name__)

NOTICE = "market_license_expiring"  # evo_agents.hub.runs.NOTICE_KINDS
ORDER_NUMBER = re.compile(tables_seller.ORDER_NUMBER)
LAPSED = "licence lapsed"  # how plugins.disabled_reason of a plugin turned off by its licence starts
MARKET_PAGE = "/admin/market"
NO_SEALER = (
    "buying a plugin needs EVO_HUB_SECRETS_KEY: the hub seals the order's secret with it; an operator sets it "
    "(`evo-agents hub secrets-key`)"
)
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422, 502, 503)}

router = APIRouter(prefix="/v1/market", tags=["market"], responses={401: {"model": ErrorBody}})


class HubIdentity(BaseModel):
    hub: str = Field(description="the hub's stable id, which a seller's licence names")
    url: str | None = Field(None, description="EVO_HUB_PUBLIC_URL, which an order carries")


class BuyIn(BaseModel):
    months: Annotated[int, Field(ge=1, le=tables_seller.MAX_MONTHS)]
    contact: Contact
    trust_publisher: bool = Field(
        False, description="trust the signer for the publisher from now on, when the hub does not yet"
    )


class Purchase(BaseModel):
    id: int
    seller: str
    seller_url: str
    plugin: str
    publisher: str
    number: str
    months: int
    usd_total: float
    state: str = Field(description="open until the seller marks it paid, paid, or cancelled")
    placed_by: str | None = None
    placed_at: datetime
    checked_at: datetime | None = None
    last_error: str | None = None
    license_id: str | None = None
    valid_from: date | None = None
    valid_until: date | None = Field(None, description="the first day the licence no longer covers")
    license_state: str | None = Field(None, description="pending, active, grace or lapsed, today")


class Bought(BaseModel):
    purchase: Purchase
    instructions: str = Field(description="how to pay the order, as the seller says")
    trusted_publisher: bool


class Purchases(BaseModel):
    hub: str
    purchases: list[Purchase]


class Checked(BaseModel):
    checked: int = Field(description="the open orders the hub asked their sellers about")
    received: list[str] = Field(description="the licences received")
    installed: list[str] = Field(description="the plugins installed or turned on again")
    reminded: list[str]
    lapsed: list[str] = Field(description="the plugins turned off")
    errors: list[str]


@dataclass(frozen=True)
class Placer:
    """The admin who ordered a plugin, as whom the hub installs it from the worker."""

    user_id: int
    login: str
    token_id: int | None = None
    admin: bool = True


def today_utc() -> date:
    """Today in UTC, as the licences count days; a test moves it."""
    return datetime.now(UTC).date()


# The hub's id


async def hub_id(conn: AsyncConnection) -> str:
    """The hub's stable id, made the first time it is asked for."""
    h = tables.hub_identity
    await conn.execute(pg_insert(h).values(hub_id=f"hub_{secrets.token_hex(16)}").on_conflict_do_nothing())
    return await conn.scalar(select(h.c.hub_id))


@router.get("/hub", response_model=HubIdentity)
async def hub_identity(request: Request, user: AdminUser) -> HubIdentity:
    """The hub's stable id, which a seller's licence names."""
    async with request.app.state.engine.begin() as conn:
        found = await hub_id(conn)
    return HubIdentity(hub=found, url=request.app.state.config.public_url)


# Buying


def _seller_of(index: dict, item: dict) -> dict:
    price = item.get("price") or {}
    if price.get("kind") == "free":
        raise HTTPException(409, f"{item['id']} is free: install it with `evo-agents hub market install`")
    seller = next((s for s in index.get("sellers") or () if s["id"] == price.get("seller")), None)
    if seller is None:
        raise HTTPException(409, f"the market lists no seller {price.get('seller')} for {item['id']}")
    return seller


def _purchase_model(row, today: date) -> Purchase:
    state = None
    if row.license_id is not None:
        state = lic.License(
            license=row.license_id,
            plugin=row.plugin,
            publisher=row.publisher,
            seller=row.seller,
            hub="",
            order=row.number,
            key=row.key_id,
            valid_from=row.valid_from,
            valid_until=row.valid_until,
            issued="",
        ).state(today)
    return Purchase(
        id=row.id,
        seller=row.seller,
        seller_url=row.seller_url,
        plugin=row.plugin,
        publisher=row.publisher,
        number=row.number,
        months=row.months,
        usd_total=float(row.usd_total),
        state=row.state,
        placed_by=row.login,
        placed_at=row.placed_at,
        checked_at=row.checked_at,
        last_error=row.last_error,
        license_id=row.license_id,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
        license_state=state,
    )


def _purchases_query():
    p, users = tables.market_purchases, tables.users
    return select(p, users.c.login).join_from(p, users, users.c.id == p.c.placed_by, isouter=True)


def _sealer(request: Request) -> Sealer:
    sealer = request.app.state.sealer
    if sealer is None:
        raise HTTPException(503, NO_SEALER)
    return sealer


@router.post("/{source}/{plugin}/buy", response_model=Bought, status_code=201, responses=REFUSALS)
async def buy(request: Request, source: SourceId, plugin: PluginId, body: BuyIn, user: AdminUser) -> Bought:
    """Order a paid plugin of a market from its seller; the hub shows the order's number and how to pay it."""
    state = request.app.state
    sealer = _sealer(request)
    async with state.engine.begin() as conn:
        row = await source_row(conn, source)
        item = entry_of(row, plugin)
        seller = _seller_of(row.index, item)
        hub = await hub_id(conn)
    if item["tier"] != "isolated":
        raise HTTPException(409, f"{plugin} is a core plugin: it ships in the hub's image")
    publisher, signer = item["publisher"]["id"], item["publisher"]["signer"]
    _, new = await check_trust(state.engine, state.config, publisher, signer, body.trust_publisher)
    client = SellerClient(seller["url"], transport=getattr(state, "seller_transport", None))
    try:
        placed = await asyncio.to_thread(
            client.place_order, plugin, hub, state.config.public_url, body.months, body.contact.model_dump()
        )
    except SellerUnavailable as exc:
        raise HTTPException(502, f"{exc}; nothing was ordered") from None
    finally:
        client.close()
    number = str(placed.get("number") or "")
    if not ORDER_NUMBER.fullmatch(number) or not isinstance(placed.get("secret"), str):
        raise HTTPException(502, f"the seller at {seller['url']} answered no order number; nothing was ordered")
    p = tables.market_purchases
    async with state.engine.begin() as conn:
        if new:
            await trust(conn, user, state.config, publisher, signer, f"buying {plugin}")
        made = await conn.execute(
            insert(p)
            .values(
                source_id=source,
                seller=seller["id"],
                seller_url=seller["url"],
                plugin=plugin,
                publisher=publisher,
                signer=signer,
                number=number,
                months=body.months,
                usd_total=placed.get("usd_total") or 0,
                secret=sealer.seal_stored(placed["secret"], market_purchase_aad(seller["url"], number)),
                placed_by=user.user_id,
            )
            .returning(p.c.id)
        )
        purchase_id = made.scalar_one()
        target = f"order:{number} seller:{seller['id']} plugin={plugin} months={body.months}"
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=audit.MARKET_ORDER_PLACE, target=target
        )
        found = (await conn.execute(_purchases_query().where(p.c.id == purchase_id))).one()
    log.info("paid plugin ordered", extra={"plugin": plugin, "order": number})
    instructions = str(placed.get("instructions") or "")[:2000]
    return Bought(purchase=_purchase_model(found, today_utc()), instructions=instructions, trusted_publisher=new)


@router.get("/licenses", response_model=Purchases)
async def purchases(request: Request, user: AdminUser) -> Purchases:
    """The hub's orders of paid plugins and their licences, the newest first."""
    p = tables.market_purchases
    async with request.app.state.engine.begin() as conn:
        hub = await hub_id(conn)
        rows = (await conn.execute(_purchases_query().order_by(p.c.id.desc()))).all()
    today = today_utc()
    return Purchases(hub=hub, purchases=[_purchase_model(row, today) for row in rows])


@router.post("/licenses/check", response_model=Checked, responses=REFUSALS)
async def check_now(request: Request, user: AdminUser) -> Checked:
    """Ask the sellers about the open orders now, install what a licence covers, and turn off what lapsed."""
    state = request.app.state
    return await check_licenses(
        Installing.of(request), _sealer(request), transport=getattr(state, "seller_transport", None)
    )


# Checking


def covers(row, today: date) -> bool:
    """Whether licence row ``row`` lets the plugin run ``today``: from its start to GRACE_DAYS after its end."""
    return row.valid_from is not None and row.valid_from <= today < row.valid_until + timedelta(days=lic.GRACE_DAYS)


async def licence_problem(conn: AsyncConnection, plugin: str, today: date | None = None) -> str | None:
    """Why the store cannot install plugin ``plugin``: the hub bought it and no licence covers today; None otherwise."""
    p = tables.market_purchases
    rows = (await conn.execute(select(p).where(p.c.plugin == plugin, p.c.state == "paid"))).all()
    if not rows:
        return None
    today = today or today_utc()
    if any(covers(row, today) for row in rows):
        return None
    return f"plugin {plugin} is sold by subscription and no licence of this hub covers today: renew it in the market"


async def _seller_keys(conn: AsyncConnection, row) -> dict[str, bytes]:
    """The keys the markets' indexes list for the seller of purchase ``row``."""
    keys: dict[str, bytes] = {}
    for source in await source_rows(conn):
        for seller in (source.index or {}).get("sellers") or ():
            if seller.get("id") == row.seller and seller.get("url") == row.seller_url:
                keys.update(lic.seller_keys(seller))
    return keys


def _download(client: SellerClient, row, token: str, hub: str, sigstore) -> CheckedRelease:
    """The newest bundle of purchase ``row``'s plugin, from its seller, checked against its publisher and signer."""
    found, data = client.bundle(row.plugin, token, hub)
    try:
        manifest = read_bundle(data).manifest
    except BundleError as exc:
        raise SellerUnavailable(f"the bundle of {row.plugin} the seller sent does not read: {exc}") from None
    if manifest.id != row.plugin or manifest.publisher != row.publisher:
        raise SellerUnavailable(f"the seller sent {manifest.id} of {manifest.publisher}, not {row.plugin}")
    signature = found.get("signature")
    signed = verify(data, signature, row.publisher, (signer_publisher(row.publisher, row.signer),), sigstore)
    return CheckedRelease(tag=manifest.version, manifest=manifest, data=data, signature=signature, signed=signed)


async def _note(engine: AsyncEngine, purchase_id: int, error: str | None) -> None:
    p = tables.market_purchases
    async with engine.begin() as conn:
        await conn.execute(
            update(p)
            .where(p.c.id == purchase_id)
            .values(checked_at=func.now(), last_error=None if error is None else error[: tables_seller.MAX_ERROR_CHARS])
        )


async def _poll(hub: Installing, sealer: Sealer, transport, hub_ref: str, row, done: Checked) -> None:
    """Ask the seller about open order ``row``; keep its licence once paid and checked."""
    p = tables.market_purchases
    try:
        secret = sealer.open_stored(row.secret, market_purchase_aad(row.seller_url, row.number))
    except Unsealable:
        await _note(hub.engine, row.id, "the order's secret does not open with this hub's key")
        return
    client = SellerClient(row.seller_url, transport=transport)
    try:
        answer = await asyncio.to_thread(client.order, row.number, secret)
    except SellerUnavailable as exc:
        done.errors.append(f"{row.number}: {exc}")
        await _note(hub.engine, row.id, str(exc))
        return
    finally:
        client.close()
    if answer.get("state") == "cancelled":
        async with hub.engine.begin() as conn:
            await conn.execute(
                update(p).where(p.c.id == row.id).values(state="cancelled", checked_at=func.now(), last_error=None)
            )
        return
    token = answer.get("license")
    if answer.get("state") != "paid" or not isinstance(token, str):
        await _note(hub.engine, row.id, None)
        return
    async with hub.engine.begin() as conn:
        keys = await _seller_keys(conn, row)
        try:
            granted = lic.read_license(token, keys, hub=hub_ref, plugin=row.plugin)
            if granted.order != row.number or granted.publisher != row.publisher or granted.seller != row.seller:
                raise lic.LicenseError("not_ours", f"licence {granted.license} is not of order {row.number}")
        except (lic.LicenseError, SignatureUnavailable) as exc:
            reason = getattr(exc, "reason", "unavailable")
            error = f"the licence of order {row.number} is refused ({reason}): {exc}"
            done.errors.append(error)
            await conn.execute(update(p).where(p.c.id == row.id).values(checked_at=func.now(), last_error=error))
            return
        await conn.execute(
            update(p)
            .where(p.c.id == row.id)
            .values(
                state="paid",
                license_id=granted.license,
                valid_from=granted.valid_from,
                valid_until=granted.valid_until,
                key_id=granted.key,
                token=token,
                received_at=func.now(),
                checked_at=func.now(),
                last_error=None,
            )
        )
        target = (
            f"license:{granted.license} order:{row.number} plugin={row.plugin} from={granted.valid_from} "
            f"until={granted.valid_until}"
        )
        await audit.record(conn, actor_id=None, token_id=None, action=audit.MARKET_LICENSE_RECEIVE, target=target)
    done.received.append(granted.license)


async def _install(hub: Installing, transport, hub_ref: str, row, done: Checked) -> None:
    """Install, or turn on again, the plugin of licence row ``row``, which covers today."""
    async with hub.engine.begin() as conn:
        plugin = await find(conn, row.plugin, lock=True)
        if plugin is not None and plugin.installed_version is not None:
            if plugin.disabled_reason is None or not plugin.disabled_reason.startswith(LAPSED):
                return  # running, or turned off for another reason that a hub admin settles
            placer = await _placer(conn, row)
            await set_installed(conn, placer, plugin, plugin.installed_version, upgrade=False)
            done.installed.append(row.plugin)
            return
        placer = await _placer(conn, row)
    client = SellerClient(row.seller_url, transport=transport)
    try:
        checked = await asyncio.to_thread(_download, client, row, row.token, hub_ref, hub.sigstore)
    except (SellerUnavailable, SignatureError, SignatureUnavailable, ValueError) as exc:
        done.errors.append(f"{row.plugin}: {exc}")
        await _note(hub.engine, row.id, f"installing {row.plugin}: {exc}")
        return
    finally:
        client.close()
    item = {"id": row.plugin, "publisher": {"id": row.publisher, "signer": row.signer}}
    try:
        await install_checked(hub, placer, item, checked, confirmed=False)
    except HTTPException as exc:
        done.errors.append(f"{row.plugin}: {exc.detail}")
        await _note(hub.engine, row.id, f"installing {row.plugin}: {exc.detail}")
        return
    done.installed.append(row.plugin)


async def _placer(conn: AsyncConnection, row) -> Placer:
    users = tables.users
    login = await conn.scalar(select(users.c.login).where(users.c.id == row.placed_by))
    return Placer(row.placed_by, login or "")


async def _remind(conn: AsyncConnection, row, admins: list[int]) -> None:
    p = tables.market_purchases
    await conn.execute(update(p).where(p.c.id == row.id).values(reminded_at=func.now()))
    body = (
        f"The licence of {row.plugin} (order {row.number} with {row.seller}) ends on {row.valid_until.isoformat()}. "
        f"The hub keeps it on for {lic.GRACE_DAYS} days more, then turns it off; renew it in the market."
    )
    for user_id in admins:
        await notify(
            conn,
            user_id=user_id,
            kind="notice",
            notice_kind=NOTICE,
            title=f"The licence of {row.plugin} ends on {row.valid_until.isoformat()}",
            body=body,
            details={"plugin": row.plugin, "order": row.number, "valid_until": row.valid_until.isoformat()},
            link=MARKET_PAGE,
        )


async def _lapse(conn: AsyncConnection, plugin, last, admins: list[int]) -> None:
    plugins = tables.plugins
    reason = f"{LAPSED}: the last licence of {plugin.name} ended on {last.valid_until.isoformat()}"
    await conn.execute(
        update(plugins)
        .where(plugins.c.id == plugin.id)
        .values(disabled_reason=reason[:MAX_REASON_CHARS], disabled_at=func.now())
    )
    await revoke_tokens(conn, plugin.id)
    target = f"license:{last.license_id} plugin={plugin.name} until={last.valid_until.isoformat()}"
    await audit.record(conn, actor_id=None, token_id=None, action=audit.MARKET_LICENSE_LAPSE, target=target)
    for user_id in admins:
        await notify(
            conn,
            user_id=user_id,
            kind="notice",
            notice_kind=DISABLED_NOTICE,
            title=f"Plugin {plugin.name} turned off: its licence lapsed",
            body=(
                f"{reason}. The hub turned it off in every project and keeps its config; renew it in the market and "
                "the hub turns it on again."
            ),
            details={"plugin": plugin.name, "version": plugin.installed_version, "license": last.license_id},
            link=ADMIN_PAGE,
        )


async def _follow_up(hub: Installing, transport, hub_ref: str, today: date, done: Checked) -> None:
    """Install what a licence covers, remind of what ends soon and turn off what lapsed, plugin by plugin."""
    p = tables.market_purchases
    async with hub.engine.begin() as conn:
        rows = (await conn.execute(select(p).where(p.c.state == "paid").order_by(p.c.plugin, p.c.valid_until))).all()
    by_plugin: dict[str, list] = {}
    for row in rows:
        by_plugin.setdefault(row.plugin, []).append(row)
    for name, licences in by_plugin.items():
        covering = [row for row in licences if covers(row, today)]
        if covering:
            await _install(hub, transport, hub_ref, covering[-1], done)
        last = max(licences, key=lambda row: row.valid_until)
        async with hub.engine.begin() as conn:
            plugin = await find(conn, name, lock=True)
            if plugin is None or plugin.installed_version is None:
                continue
            admins = await hub_admin_ids(conn, hub.config.admins)
            remind_from = last.valid_until - timedelta(days=lic.REMIND_DAYS)
            if remind_from <= today < last.valid_until and last.reminded_at is None and plugin.disabled_reason is None:
                await _remind(conn, last, admins)
                done.reminded.append(name)
            if not covering and plugin.disabled_reason is None:
                await _lapse(conn, plugin, last, admins)
                done.lapsed.append(name)


async def check_licenses(
    hub: Installing, sealer: Sealer | None, *, transport=None, today: date | None = None
) -> Checked:
    """Ask the sellers about the hub's open orders, keep the licences they issued, install what a licence covers,
    remind the hub admins of a licence that ends soon, and turn off a plugin whose licence lapsed."""
    today = today or today_utc()
    done = Checked(checked=0, received=[], installed=[], reminded=[], lapsed=[], errors=[])
    p = tables.market_purchases
    async with hub.engine.begin() as conn:
        hub_ref = await hub_id(conn)
        open_rows = (await conn.execute(select(p).where(p.c.state == "open").order_by(p.c.id))).all()
    if sealer is not None:
        for row in open_rows:
            done.checked += 1
            await _poll(hub, sealer, transport, hub_ref, row, done)
    await _follow_up(hub, transport, hub_ref, today, done)
    if done.received or done.installed or done.lapsed:
        log.info(
            "paid plugin licences checked",
            extra={"received": len(done.received), "installed": len(done.installed), "lapsed": len(done.lapsed)},
        )
    return done
