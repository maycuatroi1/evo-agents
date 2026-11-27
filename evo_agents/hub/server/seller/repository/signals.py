"""The SQL of the seller's signals: the project they go to, its first admin, the signal keys registered there, the
ingest key they come through, and the licences that cover a day."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.seller.repository.orders import active


async def project_id(conn: AsyncConnection, name: str) -> int | None:
    p = tables.projects
    return await conn.scalar(select(p.c.id).where(p.c.name == name))


async def first_admin(conn: AsyncConnection, project: int) -> int | None:
    g = tables.grants
    return await conn.scalar(
        select(g.c.user_id).where(g.c.project_id == project, g.c.role == "admin").order_by(g.c.user_id).limit(1)
    )


async def register_key(conn: AsyncConnection, **values) -> None:
    """A number key of the project's registry of signals, kept once."""
    await conn.execute(pg_insert(tables.signal_keys).values(kind="number", **values).on_conflict_do_nothing())


async def ingest_keys(conn: AsyncConnection, project: int, name: str) -> Sequence[Any]:
    """(id, max_level, revoked_at) of the project's ingest keys named ``name``."""
    k = tables.signal_ingest_keys
    query = select(k.c.id, k.c.max_level, k.c.revoked_at).where(k.c.project_id == project, k.c.name == name)
    return (await conn.execute(query)).all()


async def add_ingest_key(conn: AsyncConnection, **values):
    """(id, max_level) of the ingest key added."""
    k = tables.signal_ingest_keys
    made = await conn.execute(insert(k).values(**values).returning(k.c.id, k.c.max_level))
    return made.one()


async def active_count(conn: AsyncConnection, today: date) -> int | None:
    """How many licences cover ``today``."""
    return await conn.scalar(select(func.count()).where(active(today)))
