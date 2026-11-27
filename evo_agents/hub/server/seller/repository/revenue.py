"""The SQL of the seller's revenue: the gross of the orders paid in a month by publisher, the payouts of that month,
and marking a payout."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def gross_by_publisher(conn: AsyncConnection, start: datetime, end: datetime) -> Sequence[Any]:
    """(publisher, orders, gross) of the orders paid from ``start`` to ``end``, by publisher."""
    o, m = tables.market_orders, tables.market_listings
    query = (
        select(m.c.publisher, func.count().label("orders"), func.sum(o.c.usd_total).label("gross"))
        .join_from(o, m, m.c.id == o.c.listing_id)
        .where(o.c.state == "paid", o.c.closed_at >= start, o.c.closed_at < end)
        .group_by(m.c.publisher)
        .order_by(m.c.publisher)
    )
    return (await conn.execute(query)).all()


async def payouts(conn: AsyncConnection, month: date) -> dict:
    """publisher -> its payout of ``month``, with the login of who marked it."""
    p, users = tables.market_payouts, tables.users
    paid = (
        select(p, users.c.login).join_from(p, users, users.c.id == p.c.paid_by, isouter=True).where(p.c.month == month)
    )
    return {row.publisher: row for row in await conn.execute(paid)}


async def add_payout(conn: AsyncConnection, **values) -> None:
    await conn.execute(insert(tables.market_payouts).values(**values))


async def paid_total(conn: AsyncConnection, start: datetime, end: datetime):
    """The sum of the orders paid from ``start`` to ``end``, 0 for none."""
    o = tables.market_orders
    return await conn.scalar(
        select(func.coalesce(func.sum(o.c.usd_total), 0)).where(
            o.c.state == "paid", o.c.closed_at >= start, o.c.closed_at < end
        )
    )
