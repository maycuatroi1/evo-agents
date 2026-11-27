"""The SQL of the seller's listings and their bundles: reading them with their owner, their uploaders and the licences
that cover today, and adding, changing and uploading to them."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.seller.repository.orders import active


async def listing(conn: AsyncConnection, plugin: str, *, lock: bool = False):
    """The listing of plugin ``plugin``, or None."""
    m = tables.market_listings
    query = select(m).where(m.c.plugin == plugin)
    if lock:
        query = query.with_for_update()
    return (await conn.execute(query)).first()


async def listings(
    conn: AsyncConnection, today: date, plugin: str | None = None
) -> tuple[Sequence[Any], dict[int, list]]:
    """The listings (``plugin``'s alone, when given) with their owner and the licences of each that cover ``today``,
    and the bundles of each by listing id, the newest first, with their uploader."""
    m, b, o, users = tables.market_listings, tables.market_bundles, tables.market_orders, tables.users
    uploader = users.alias("uploader")
    covering = (
        select(func.count()).where(o.c.listing_id == m.c.id, active(today)).scalar_subquery().label("active_licenses")
    )
    query = (
        select(m, users.c.login.label("owner"), covering)
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
        bundles.setdefault(row.listing_id, []).append(row)
    return rows, bundles


async def add_listing(conn: AsyncConnection, **values) -> bool:
    """Whether the listing was added: False when its plugin is listed already."""
    m = tables.market_listings
    made = await conn.execute(pg_insert(m).values(**values).on_conflict_do_nothing().returning(m.c.id))
    return made.first() is not None


async def change_listing(conn: AsyncConnection, listing_id: int, **values) -> None:
    m = tables.market_listings
    await conn.execute(update(m).where(m.c.id == listing_id).values(**values, updated_at=func.now()))


async def bundle_count(conn: AsyncConnection, listing_id: int) -> int:
    b = tables.market_bundles
    return int(await conn.scalar(select(func.count()).where(b.c.listing_id == listing_id)) or 0)


async def bundle_sha256(conn: AsyncConnection, listing_id: int, version: str) -> str | None:
    """The SHA-256 of version ``version`` of the listing, when it is uploaded."""
    b = tables.market_bundles
    same = (await conn.execute(select(b.c.sha256).where(b.c.listing_id == listing_id, b.c.version == version))).first()
    return None if same is None else same.sha256


async def add_bundle(conn: AsyncConnection, sha256: str, size: int, user_id: int, **values) -> None:
    """The blob row of a plugin bundle of the hub's, kept once, and the listing's bundle of it."""
    b, blobs = tables.market_bundles, tables.blobs
    await conn.execute(
        pg_insert(blobs)
        .values(project_id=None, sha256=sha256, size=size, kind="plugin-bundle", created_by=user_id)
        .on_conflict_do_nothing()
    )
    await conn.execute(insert(b).values(sha256=sha256, size=size, uploaded_by=user_id, **values))
