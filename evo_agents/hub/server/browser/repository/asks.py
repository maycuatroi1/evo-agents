"""What a login asked by a run's agent reads: the run's browser identities and its open login decision."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def run_identities(conn: AsyncConnection, run_id: int) -> list:
    """The id and name of each browser identity run ``run_id`` was dispatched with, by id."""
    r, b = tables.runs, tables.browser_identities
    held = (await conn.execute(select(r.c.browser_identities).where(r.c.id == run_id))).scalar_one() or []
    return list((await conn.execute(select(b.c.id, b.c.name).where(b.c.id.in_(held)).order_by(b.c.id))).all())


async def open_decision(conn: AsyncConnection, run_id: int, category: str) -> int | None:
    """The id of an open decision of ``category`` of run ``run_id``, if there is one."""
    d = tables.decisions
    open_one = select(d.c.id).where(d.c.run_id == run_id, d.c.category == category, d.c.state == "open")
    return (await conn.execute(open_one)).scalars().first()
