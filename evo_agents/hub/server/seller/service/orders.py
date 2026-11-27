"""The orders buying hubs placed (``evo_agents.hub.server.seller.service.keys`` says what each route does): reading
them, expiring those left open ORDER_TTL, and marking one paid, which issues its licence, or cancelled."""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, date, datetime, timedelta

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.config import HubConfig
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.errors import Conflict, NotFound
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.seller.models.seller import ORDER_TTL, Order, OrderPaidIn, Orders
from evo_agents.hub.server.seller.repository import orders as repository
from evo_agents.hub.server.seller.service.keys import seller_key
from evo_agents.hub.server.seller.service.shared import record, usd
from evo_agents.hub.server.seller.service.signals import push_signals_logged
from evo_agents.plugins import license as lic

log = logging.getLogger(__name__)


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
        usd_per_month=usd(row.usd_per_month),
        usd_total=usd(row.usd_total),
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


async def expire_orders(conn: AsyncConnection) -> None:
    """Cancel the orders left open ORDER_TTL or longer, in the name of the owner of the listing each orders (a closed
    order names who closed it), each audited."""
    for row in await repository.expire(conn, ORDER_TTL):
        target = f"order:{row.number} hub:{row.buyer_hub} expired"
        await audit.record(conn, actor_id=None, token_id=None, action=audit.MARKET_ORDER_CANCEL, target=target)


async def order_row(conn: AsyncConnection, number: str, *, lock: bool = False):
    await expire_orders(conn)
    row = await repository.order(conn, number, lock=lock)
    if row is None:
        raise NotFound(f"this hub has no order {number}: see `evo-agents hub market orders`")
    return row


async def orders(engine: AsyncEngine, state: str | None) -> Orders:
    """The orders, the newest first; those in ``state`` alone, when given."""
    async with UnitOfWork(engine) as conn:
        await expire_orders(conn)
        return Orders(orders=[order_model(row) for row in await repository.orders(conn, state)])


async def licence_start(conn: AsyncConnection, row, today: date) -> date:
    """Where the licence of order ``row`` starts: the end of the buying hub's last licence of the plugin when that has
    not lapsed (a renewal runs on from it), else today."""
    last = await repository.last_valid_until(conn, row)
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
        seller=config.seller_id,  # pyright: ignore[reportArgumentType]
        hub=row.buyer_hub,
        order=row.number,
        key=found,
        valid_from=start,
        valid_until=lic.add_months(start, row.months),
        issued=issued.isoformat(timespec="seconds"),
    )
    return granted, lic.sign_license(granted, config.seller_key)  # pyright: ignore[reportArgumentType]


def _open(row, number: str) -> None:
    if row.state != "open":
        raise Conflict(f"order {number} is {row.state}, not open")


async def mark_paid(request: Request, number: str, body: OrderPaidIn, user: Principal) -> Order:
    """Mark open order ``number`` paid with the invoice number and issue its licence, then write the signals."""
    config = request.app.state.config
    seller_key(config)
    async with request.app.state.engine.begin() as conn:
        row = await order_row(conn, number, lock=True)
        _open(row, number)
        now = datetime.now(UTC)
        start = await licence_start(conn, row, now.date())
        granted, token = sign(config, row, start, now)
        await repository.close(
            conn,
            row.id,
            state="paid",
            invoice=body.invoice,
            closed_by=user.user_id,
            license_id=granted.license,
            valid_from=granted.valid_from,
            valid_until=granted.valid_until,
            key_id=granted.key,
            token=token,
            issued_at=now,
        )
        await record(conn, user, audit.MARKET_ORDER_PAID, f"order:{number} hub:{row.buyer_hub} invoice={body.invoice}")
        target = (
            f"license:{granted.license} order:{number} hub:{row.buyer_hub} plugin={row.plugin} "
            f"until={granted.valid_until.isoformat()}"
        )
        await record(conn, user, audit.MARKET_LICENSE_ISSUE, target)
        result = order_model(await order_row(conn, number))
    log.info("order paid, licence issued", extra={"order": number, "license": granted.license})
    await push_signals_logged(request.app.state.engine, config)
    return result


async def cancel_order(engine: AsyncEngine, number: str, user: Principal) -> Order:
    """Cancel open order ``number``."""
    async with UnitOfWork(engine) as conn:
        row = await order_row(conn, number, lock=True)
        _open(row, number)
        await repository.close(conn, row.id, state="cancelled", closed_by=user.user_id)
        await record(conn, user, audit.MARKET_ORDER_CANCEL, f"order:{number} hub:{row.buyer_hub}")
        return order_model(await order_row(conn, number))
