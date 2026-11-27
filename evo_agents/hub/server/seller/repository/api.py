"""The SQL of the seller's API for buying hubs: the listed plugins and their versions, the order lock and the next
order number, the open orders counted, placing an order, and the order and bundles a licence reaches."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import BigInteger, func, insert, literal, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.seller.models.api import ORDER_LOCK


async def listed(conn: AsyncConnection) -> tuple[Sequence[Any], dict[int, list[str]]]:
    """The listings for sale, by plugin, and the versions uploaded of every listing, by its id."""
    m, b = tables.market_listings, tables.market_bundles
    rows = (await conn.execute(select(m).where(m.c.state == "listed").order_by(m.c.plugin))).all()
    versions: dict[int, list[str]] = {}
    for row in await conn.execute(select(b.c.listing_id, b.c.version)):
        versions.setdefault(row.listing_id, []).append(row.version)
    return rows, versions


async def next_number(conn: AsyncConnection, year: int) -> str:
    """The next order number of ``year``, under the order lock."""
    o = tables.market_orders
    await conn.execute(select(func.pg_advisory_xact_lock(literal(ORDER_LOCK, BigInteger))))
    held = await conn.scalar(select(func.count()).where(o.c.number.like(f"ORD-{year}-%")))
    return f"ORD-{year}-{int(held or 0) + 1:04d}"


async def open_counts(conn: AsyncConnection, address: str) -> tuple[int, int]:
    """The orders open from ``address`` and in all."""
    o = tables.market_orders
    open_orders = select(func.count()).where(o.c.state == "open")
    from_address = await conn.scalar(open_orders.where(o.c.buyer_address == address))
    return int(from_address or 0), int(await conn.scalar(open_orders) or 0)


async def listing_of(conn: AsyncConnection, plugin: str):
    m = tables.market_listings
    return (await conn.execute(select(m).where(m.c.plugin == plugin))).first()


async def add_order(conn: AsyncConnection, **values) -> None:
    await conn.execute(insert(tables.market_orders).values(**values))


async def order_with_plugin(conn: AsyncConnection, number: str):
    """Order ``number`` with the plugin of its listing, or None."""
    o, m = tables.market_orders, tables.market_listings
    query = select(o, m.c.plugin).join_from(o, m, m.c.id == o.c.listing_id).where(o.c.number == number)
    return (await conn.execute(query)).first()


async def order_of_licence(conn: AsyncConnection, license_id: str):
    """(state, buyer_hub, valid_from, valid_until, listing_id) of the order of licence ``license_id``, or None."""
    o = tables.market_orders
    query = select(o.c.state, o.c.buyer_hub, o.c.valid_from, o.c.valid_until, o.c.listing_id).where(
        o.c.license_id == license_id
    )
    return (await conn.execute(query)).first()


async def listing_id(conn: AsyncConnection, listing: int, plugin: str) -> int | None:
    """``listing`` when it is the listing of plugin ``plugin``, else None."""
    m = tables.market_listings
    found = (await conn.execute(select(m.c.id).where(m.c.id == listing, m.c.plugin == plugin))).first()
    return None if found is None else found.id


async def bundles(conn: AsyncConnection, listing: int) -> Sequence[Any]:
    b = tables.market_bundles
    return (await conn.execute(select(b).where(b.c.listing_id == listing))).all()
