"""Every query of the figures the web's autonomy settings page shows: the ledger's decisions of a principal, the
decisions their runs asked, and the merges of their runs with those reverted or rolled back."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import post_merge, tables
from evo_agents.hub.server import audit


async def decided(conn: AsyncConnection, user_id: int, since) -> list[str]:
    """The targets of the ledger's ``autonomy.decided`` rows whose principal is ``user_id``, since ``since``."""
    a = tables.audit
    query = select(a.c.target).where(a.c.action == audit.AUTONOMY_DECIDED, a.c.actor_id == user_id, a.c.at >= since)
    return list((await conn.execute(query)).scalars())


async def asked(conn: AsyncConnection, user_id: int, since, categories: list[str]) -> list[tuple[str, int]]:
    """(category, count) of the decisions of ``categories`` the runs of ``user_id`` asked since ``since``."""
    d, r = tables.decisions, tables.runs
    query = (
        select(d.c.category, func.count())
        .join_from(d, r, r.c.id == d.c.run_id)
        .where(r.c.principal_id == user_id, d.c.asked_at >= since, d.c.category.in_(categories))
        .group_by(d.c.category)
    )
    return [(category, count) for category, count in await conn.execute(query)]


async def merges(conn: AsyncConnection, user_id: int, since):
    """(id, state) of the merges of the runs of ``user_id`` since ``since``, a revert aside."""
    q, r = tables.merge_queue, tables.runs
    mine = (
        select(q.c.id, q.c.state)
        .join_from(q, r, r.c.id == q.c.run_id)
        .where(r.c.principal_id == user_id, q.c.merged_at >= since, q.c.source != post_merge.REVERT)
    )
    return (await conn.execute(mine)).all()


async def rolled_back(conn: AsyncConnection, queue_ids: list[int]) -> int:
    """How many of the merges ``queue_ids`` PROD was rolled back from."""
    e = tables.merge_events
    rolled = select(func.count(func.distinct(e.c.queue_id))).where(e.c.queue_id.in_(queue_ids), e.c.kind == "rollback")
    return (await conn.execute(rolled)).scalar_one()
