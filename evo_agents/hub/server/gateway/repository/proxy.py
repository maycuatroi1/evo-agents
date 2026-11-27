"""The query of the hub's gateway route (``service.proxy``): the key row a run's token is of, while it may be used."""

from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import runs, tables
from evo_agents.hub.gateway_runs import GATEWAY


def _holder_query(digest: bytes):
    cl, r = tables.credential_leases, tables.runs
    reached = tables.credential_leases.alias("reached")
    lost = exists().where(
        reached.c.run_id == cl.c.run_id,
        reached.c.external_id == cl.c.external_id,
        reached.c.provider == GATEWAY,
        reached.c.secret_id.is_not(None),
        reached.c.revoked_at.is_not(None),
    )
    return (
        select(cl.c.id, cl.c.run_id, cl.c.sealed_value, cl.c.nonce, cl.c.key_id, r.c.gateway_provider, r.c.project_id)
        .join_from(cl, r, r.c.id == cl.c.run_id)
        .where(
            cl.c.token_hash == digest,
            cl.c.provider == GATEWAY,
            cl.c.secret_id.is_(None),
            cl.c.revoked_at.is_(None),
            cl.c.sealed_value.is_not(None),
            r.c.state.in_(runs.HELD_STATES),
            r.c.worker_id == cl.c.worker_id,
            ~lost,
        )
    )


async def holder_row(engine: AsyncEngine, digest: bytes):
    """The key row whose token hash is ``digest``, not revoked and holding its key, of a run held on the worker the
    lease was made for, none of whose accounts' leases is revoked; None without one."""
    async with engine.connect() as conn:
        return (await conn.execute(_holder_query(digest))).first()
