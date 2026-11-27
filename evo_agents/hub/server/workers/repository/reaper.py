"""The queries of the pool node reaper: the live pool nodes offline longer than their pool lets them be."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


def stale():
    """The live pool nodes offline longer than their pool lets them be, as (id, name, owner login, token id, days)."""
    w, pools, users = tables.workers, tables.worker_pools, tables.users
    seen = func.coalesce(w.c.last_heartbeat_at, w.c.created_at)
    return (
        select(w.c.id, w.c.name, users.c.login, w.c.token_id, pools.c.reap_after_days)
        .join_from(w, pools, pools.c.id == w.c.pool_id)
        .join(users, users.c.id == w.c.owner_id)
        .where(
            w.c.pool_id.is_not(None),
            w.c.revoked_at.is_(None),
            seen < func.now() - func.make_interval(0, 0, 0, pools.c.reap_after_days),
        )
    )


async def stale_nodes(conn: AsyncConnection, batch: int) -> list:
    """At most ``batch`` stale nodes, by id."""
    return list((await conn.execute(stale().order_by(tables.workers.c.id).limit(batch))).all())


async def lock_stale(conn: AsyncConnection, worker_id: int):
    """Node ``worker_id`` as ``stale`` shows it, its row locked, when it is stale still; None otherwise."""
    w = tables.workers
    return (await conn.execute(stale().where(w.c.id == worker_id).with_for_update(of=w))).one_or_none()
