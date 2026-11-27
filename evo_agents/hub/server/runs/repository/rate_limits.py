"""The rows a usage report of a run's account reads and writes (``evo_agents.hub.server.rate_limits``): the run's lease,
the account's last usage, and the lease's revocation."""

from __future__ import annotations

from sqlalchemy import and_, case, func, not_, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def lease(conn: AsyncConnection, run_id: int, worker_id: int, lease_id: int):
    """(secret_id, target, revoked_at) of lease ``lease_id`` of an env secret run ``run_id`` holds on the worker, or
    None."""
    cl = tables.credential_leases
    query = select(cl.c.secret_id, cl.c.target, cl.c.revoked_at).where(
        cl.c.id == lease_id,
        cl.c.run_id == run_id,
        cl.c.worker_id == worker_id,
        cl.c.provider == "secret",
        cl.c.secret_id.is_not(None),
    )
    return (await conn.execute(query)).one_or_none()


def _object(value):
    """``value`` when it is a JSON object, else an empty one."""
    return case((func.jsonb_typeof(value) == "object", value), else_=func.jsonb_build_object(type_=JSONB))


def _merged(kept, new):
    """The usage ``new`` with the windows of ``kept`` it does not list: a window is replaced by name, and the usage
    holds no ``windows`` when neither lists one."""
    windows = _object(kept["windows"]).op("||", return_type=JSONB)(_object(new["windows"]))
    listed = func.jsonb_build_object("windows", func.nullif(windows, func.jsonb_build_object(type_=JSONB)), type_=JSONB)
    return func.jsonb_strip_nulls(new.op("||", return_type=JSONB)(listed), type_=JSONB)


async def keep_usage(conn: AsyncConnection, secret_id: int, usage: dict) -> None:
    """Keep ``usage`` as the account's last, window by window as ``rate_limits`` says; one not rejected leaves a rejected
    one of a cooling account alone."""
    a = tables.accounts
    statement = insert(a).values(secret_id=secret_id, last_usage=usage, last_usage_at=func.now())
    values = {"last_usage": _merged(a.c.last_usage, statement.excluded.last_usage), "last_usage_at": func.now()}
    where = None
    if usage.get("status") != "rejected":
        limited = and_(a.c.cooldown_until > func.now(), a.c.last_usage["status"].astext == "rejected")
        where = not_(func.coalesce(limited, False))
    await conn.execute(statement.on_conflict_do_update(index_elements=[a.c.secret_id], set_=values, where=where))


async def revoke(conn: AsyncConnection, lease_id: int) -> None:
    cl = tables.credential_leases
    await conn.execute(update(cl).where(cl.c.id == lease_id, cl.c.revoked_at.is_(None)).values(revoked_at=func.now()))
