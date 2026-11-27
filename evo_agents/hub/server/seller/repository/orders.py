"""The SQL of the orders buying hubs placed: reading them with their listing and who closed them, expiring the stale,
and marking one paid or cancelled."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


def active(today: date):
    """The paid orders whose licence covers ``today``."""
    o = tables.market_orders.c
    return and_(o.state == "paid", o.valid_from <= today, o.valid_until > today)


def _orders_query():
    o, m, users = tables.market_orders, tables.market_listings, tables.users
    return (
        select(o, m.c.plugin, m.c.publisher, users.c.login.label("closer"))
        .join_from(o, m, m.c.id == o.c.listing_id)
        .join_from(o, users, users.c.id == o.c.closed_by, isouter=True)
    )


async def expire(conn: AsyncConnection, ttl: timedelta) -> Sequence[Any]:
    """Cancel the orders left open ``ttl`` or longer, in the name of the owner of the listing each orders; the
    (number, buyer_hub) of each."""
    o, m = tables.market_orders, tables.market_listings
    stale = and_(o.c.state == "open", o.c.created_at < func.now() - ttl)
    owner = select(m.c.owner_id).where(m.c.id == o.c.listing_id).scalar_subquery()
    expired = update(o).where(stale).values(state="cancelled", closed_by=owner, closed_at=func.now())
    return (await conn.execute(expired.returning(o.c.number, o.c.buyer_hub))).all()


async def order(conn: AsyncConnection, number: str, *, lock: bool = False):
    """Order ``number``, or None."""
    query = _orders_query().where(tables.market_orders.c.number == number)
    if lock:
        query = query.with_for_update(of=tables.market_orders)
    return (await conn.execute(query)).first()


async def orders(conn: AsyncConnection, state: str | None = None) -> Sequence[Any]:
    """The orders, the newest first; those in ``state`` alone, when given."""
    o = tables.market_orders
    query = _orders_query().order_by(o.c.id.desc())
    if state is not None:
        query = query.where(o.c.state == state)
    return (await conn.execute(query)).all()


async def last_valid_until(conn: AsyncConnection, row) -> date | None:
    """The end of the last licence of order ``row``'s buying hub for its listing, other than its own."""
    o = tables.market_orders
    return await conn.scalar(
        select(func.max(o.c.valid_until)).where(
            o.c.buyer_hub == row.buyer_hub, o.c.listing_id == row.listing_id, o.c.state == "paid", o.c.id != row.id
        )
    )


async def close(conn: AsyncConnection, order_id: int, **values) -> None:
    """Close order ``order_id`` now with ``values`` (its state, who closed it and, when paid, its licence)."""
    o = tables.market_orders
    await conn.execute(update(o).where(o.c.id == order_id).values(**values, closed_at=func.now()))
