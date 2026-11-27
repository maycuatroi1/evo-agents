"""The queries of the signers hub admins trusted (table plugin_publishers, 0154)."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def signers(conn: AsyncConnection) -> list[tuple[str, str]]:
    """(publisher, signer) of each signer hub admins trusted and did not remove, as they trusted them."""
    p = tables.plugin_publishers.c
    query = select(p.publisher, p.signer).where(p.removed_at.is_(None)).order_by(p.id)
    return [(row.publisher, row.signer) for row in await conn.execute(query)]


async def trusted_rows(conn: AsyncConnection) -> list:
    """The signers hub admins trusted and did not remove, with the login of who did."""
    p, users = tables.plugin_publishers, tables.users
    query = (
        select(p, users.c.login)
        .join_from(p, users, users.c.id == p.c.added_by, isouter=True)
        .where(p.c.removed_at.is_(None))
        .order_by(p.c.id)
    )
    return list((await conn.execute(query)).all())


async def add_signer(conn: AsyncConnection, publisher: str, signer: str, user_id: int | None, note: str | None) -> bool:
    """Write the trust of ``signer`` for ``publisher``; False when the hub trusted it already."""
    p = tables.plugin_publishers
    inserted = await conn.execute(
        pg_insert(p)
        .values(publisher=publisher, signer=signer, added_by=user_id, note=note)
        .on_conflict_do_nothing()
        .returning(p.c.id)
    )
    return inserted.first() is not None


async def remove_signer(conn: AsyncConnection, entry_id: int, user_id: int | None):
    """Mark trusted signer ``entry_id`` removed; its (publisher, signer), or None when there is no such live row."""
    p = tables.plugin_publishers
    removed = await conn.execute(
        update(p)
        .where(p.c.id == entry_id, p.c.removed_at.is_(None))
        .values(removed_by=user_id, removed_at=func.now())
        .returning(p.c.publisher, p.c.signer)
    )
    return removed.first()
