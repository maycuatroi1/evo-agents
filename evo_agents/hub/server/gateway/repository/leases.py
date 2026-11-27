"""The queries of the lease of a run on a gateway model (``service.leases``): the run's gateway columns and its pick,
the key row it holds, the rows ``record`` writes, and the key rows the sweep takes back."""

from __future__ import annotations

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.gateway_runs import GATEWAY, PICK_PREFIX


async def gateway_run(conn: AsyncConnection, run_id: int):
    """The columns of run ``run_id`` that say how it goes through the gateway."""
    r = tables.runs
    query = select(r.c.gateway_provider, r.c.model, r.c.routing, r.c.runtime, r.c.timeout_s).where(r.c.id == run_id)
    return (await conn.execute(query)).one()


async def picked(conn: AsyncConnection, run_id: int, prefix: str) -> int | None:
    """The provider account of ``prefix`` picked by hand for the run, if any."""
    ra = tables.run_accounts
    query = select(ra.c.secret_id).where(ra.c.run_id == run_id, ra.c.env_var == PICK_PREFIX + prefix)
    return (await conn.execute(query)).scalar_one_or_none()


async def held_key(
    conn: AsyncConnection, run_id: int, worker_id: int, wanted: set[int], *, within: bool = False
) -> int | None:
    """The live key row the run holds on this worker that reaches exactly the accounts ``wanted``; with ``within``,
    some of them alone (quota-pacing narrowed the run to one: the run keeps the one it took)."""
    cl = tables.credential_leases
    live = and_(cl.c.run_id == run_id, cl.c.worker_id == worker_id, cl.c.provider == GATEWAY, cl.c.revoked_at.is_(None))
    keys = select(cl.c.id, cl.c.external_id).where(live, cl.c.secret_id.is_(None), cl.c.sealed_value.is_not(None))
    key = (await conn.execute(keys.where(cl.c.expires_at > func.now()).order_by(cl.c.id.desc()).limit(1))).first()
    if key is None:
        return None
    reached = select(cl.c.secret_id).where(live, cl.c.secret_id.is_not(None), cl.c.external_id == key.external_id)
    reaches = set((await conn.execute(reached)).scalars())
    return key.id if reaches == wanted or (within and reaches and reaches <= wanted) else None


async def insert_lease(conn: AsyncConnection, values: dict) -> int:
    cl = tables.credential_leases
    return (await conn.execute(cl.insert().values(**values).returning(cl.c.id))).scalar_one()


async def sealed_key(conn: AsyncConnection, lease_id: int):
    """The sealed key of key row ``lease_id``: (sealed_value, nonce, key_id)."""
    cl = tables.credential_leases
    return (await conn.execute(select(cl.c.sealed_value, cl.c.nonce, cl.c.key_id).where(cl.c.id == lease_id))).one()


async def expires_at(conn: AsyncConnection, lease_id: int):
    cl = tables.credential_leases
    return (await conn.execute(select(cl.c.expires_at).where(cl.c.id == lease_id))).scalar_one()


async def revoke_earlier(conn: AsyncConnection, run_id: int) -> None:
    """Mark the run's live gateway rows revoked."""
    cl = tables.credential_leases
    earlier = and_(cl.c.run_id == run_id, cl.c.provider == GATEWAY, cl.c.revoked_at.is_(None))
    await conn.execute(update(cl).values(revoked_at=func.greatest(func.now(), cl.c.issued_at)).where(earlier))


async def store_key(conn: AsyncConnection, lease_id: int, values: dict) -> None:
    """Write the sealed key and the token hash of key row ``lease_id``."""
    cl = tables.credential_leases
    await conn.execute(update(cl).values(**values).where(cl.c.id == lease_id))


async def stamp_leased(conn: AsyncConnection, secret_ids: list[int]) -> None:
    """Stamp the accounts ``secret_ids`` as leased now."""
    a = tables.accounts
    stamp = {"last_leased_at": func.now()}
    taken = pg_insert(a).values([{"secret_id": secret_id, **stamp} for secret_id in secret_ids])
    await conn.execute(taken.on_conflict_do_update(index_elements=[a.c.secret_id], set_=stamp))


async def ended_keys(conn: AsyncConnection, batch: int) -> list:
    """The key rows that still hold their key and are revoked, past their end, or of a run no longer held."""
    cl, r = tables.credential_leases, tables.runs
    over = or_(cl.c.revoked_at.is_not(None), cl.c.expires_at <= func.now(), r.c.state.not_in(runs.HELD_STATES))
    query = (
        select(cl.c.id, cl.c.run_id, cl.c.external_id)
        .join_from(cl, r, r.c.id == cl.c.run_id)
        .where(cl.c.provider == GATEWAY, cl.c.secret_id.is_(None), cl.c.sealed_value.is_not(None), over)
        .order_by(cl.c.id)
        .limit(batch)
    )
    return list((await conn.execute(query)).all())


async def drop_key(conn: AsyncConnection, row) -> bool:
    """Drop the key of key row ``row`` and end the leases of its accounts; False when another sweep took it first."""
    cl = tables.credential_leases
    ended = func.greatest(func.now(), cl.c.issued_at)
    taken = update(cl).values(
        sealed_value=None, nonce=None, key_id=None, revoked_at=func.coalesce(cl.c.revoked_at, ended)
    )
    if not (await conn.execute(taken.where(cl.c.id == row.id, cl.c.sealed_value.is_not(None)))).rowcount:
        return False
    same = and_(cl.c.external_id == row.external_id, cl.c.provider == GATEWAY, cl.c.revoked_at.is_(None))
    await conn.execute(update(cl).values(revoked_at=ended).where(same))
    return True


async def run_usage(conn: AsyncConnection, run_id: int):
    """runs.usage of run ``run_id``, locked for the update that follows."""
    r = tables.runs
    return (await conn.execute(select(r.c.usage).where(r.c.id == run_id).with_for_update())).scalar_one()


async def set_run_usage(conn: AsyncConnection, run_id: int, usage: dict) -> None:
    r = tables.runs
    await conn.execute(update(r).values(usage=usage).where(r.c.id == run_id))
