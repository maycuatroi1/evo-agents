"""The pool node reaper: the job hub.reap_pool_nodes (``reap_pool_nodes``) revokes each node of a worker pool that
has been offline longer than its pool's ``reap_after_days``, as POST /v1/workers/{id}/revoke would
(``workers.end_worker``): its token ends and its name is free again, the runs it holds are released as the reaper
releases a dead worker's runs, its leases are given back, and the worker.revoke audit row has no actor and ends
``by=reaper``. A node's time offline counts from its last heartbeat, or from its join when it never sent one.

Workers outside a pool (paired or registered: ``workers.pool_id`` NULL) are never revoked here; their owner revokes
them by hand. Each node is revoked in a transaction of its own that locks its row and checks again that it is still
live and still stale, so a node that sent a heartbeat or was revoked by hand meanwhile is left alone; the GitHub
tokens its leases held are revoked once that transaction commits (``workers.revoke_leased_tokens``)."""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import tables
from evo_agents.hub.server import credentials
from evo_agents.hub.server.workers import end_worker

log = logging.getLogger(__name__)

REAPER = "reaper"  # the by= of the audit row of a node the reaper revoked
REAP_BATCH = 200  # nodes one pass revokes at most; the next pass takes the rest


def _stale():
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


async def reap_pool_nodes(engine: AsyncEngine, sealer, github_app, batch: int = REAP_BATCH) -> dict:
    """One pass: every pool node offline past its pool's ``reap_after_days`` revoked. Returns {revoked, released}:
    how many nodes it revoked and how many of their runs it moved."""
    async with engine.connect() as conn:
        found = (await conn.execute(_stale().order_by(tables.workers.c.id).limit(batch))).all()
    revoked = released = 0
    for worker_id, *_ in found:
        async with engine.begin() as conn:
            row = (
                await conn.execute(_stale().where(tables.workers.c.id == worker_id).with_for_update(of=tables.workers))
            ).one_or_none()
            if row is None:  # it came back, or someone revoked it, since the pass looked
                continue
            _, name, owner, token_id, days = row
            why = f"was offline more than {days} day{'s' if days != 1 else ''} and the reaper revoked it"
            moved = await end_worker(conn, None, worker_id, name, owner, token_id, why=why, by=REAPER)
        log.info("pool node reaped", extra={"worker_id": worker_id, "days": days, "runs_released": moved})
        await credentials.revoke_tokens(engine, sealer, github_app, worker_id=worker_id)
        revoked, released = revoked + 1, released + moved
    return {"revoked": revoked, "released": released}
