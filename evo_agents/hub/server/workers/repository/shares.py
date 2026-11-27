"""The queries of the units a worker is narrowed to (``tables.worker_shares``) and whether it is shared."""

from __future__ import annotations

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.types import Text

from evo_agents.hub import tables
from evo_agents.hub.server.workers.repository.doctor import doctor_fresh


def listed(*where):
    """The live workers that match ``where`` with whom each is shared and its newest doctor report, by owner and
    name."""
    w, users, ws, u = tables.workers, tables.users, tables.worker_shares, tables.org_units
    units = select(u.c.slug).join_from(ws, u, u.c.id == ws.c.unit_id).where(ws.c.worker_id == w.c.id).order_by(u.c.slug)
    return (
        select(
            w.c.id,
            w.c.name.label("worker"),
            users.c.login.label("owner"),
            w.c.shared,
            func.array(units.scalar_subquery(), type_=ARRAY(Text)).label("units"),
            w.c.doctor_at,
            w.c.doctor_high,
            w.c.doctor_medium,
        )
        .join_from(w, users, users.c.id == w.c.owner_id)
        .where(w.c.revoked_at.is_(None), *where)
        .order_by(users.c.login, w.c.name, w.c.id)
    )


async def rows(conn: AsyncConnection, owner_id: int | None) -> list:
    """The live workers of ``owner_id``, every live worker for None, as ``listed`` shows them."""
    where = [] if owner_id is None else [tables.workers.c.owner_id == owner_id]
    return list(await conn.execute(listed(*where)))


async def one(conn: AsyncConnection, worker_id: int):
    return (await conn.execute(listed(tables.workers.c.id == worker_id))).one()


async def locked_worker(conn: AsyncConnection, worker_id: int):
    """(owner_id, owner login, name, shared, revoked_at, doctor_at, doctor_high, doctor_fresh) of worker
    ``worker_id``, its row locked; None for none."""
    w, users = tables.workers, tables.users
    fresh = doctor_fresh(w.c.doctor_at).label("doctor_fresh")
    query = (
        select(w.c.owner_id, users.c.login, w.c.name, w.c.shared, w.c.revoked_at, w.c.doctor_at, w.c.doctor_high, fresh)
        .join_from(w, users, users.c.id == w.c.owner_id)
        .where(w.c.id == worker_id)
        .with_for_update(of=w)
    )
    return (await conn.execute(query)).one_or_none()


async def add_unit(conn: AsyncConnection, worker_id: int, unit_id: int, created_by: int) -> bool:
    """Narrow the worker to unit ``unit_id``; False when it is narrowed to it already."""
    ws = tables.worker_shares
    added = await conn.execute(
        insert(ws)
        .values(worker_id=worker_id, unit_id=unit_id, created_by=created_by)
        .on_conflict_do_nothing(index_elements=[ws.c.worker_id, ws.c.unit_id])
        .returning(ws.c.unit_id)
    )
    return added.first() is not None


async def drop_unit(conn: AsyncConnection, worker_id: int, slug: str) -> bool:
    """Take unit ``slug`` back from the worker; False when it was not narrowed to it."""
    ws, u = tables.worker_shares, tables.org_units
    unit = select(u.c.id).where(u.c.slug == slug).scalar_subquery()
    gone = await conn.execute(delete(ws).where(ws.c.worker_id == worker_id, ws.c.unit_id == unit))
    return bool(gone.rowcount)


async def unit_count(conn: AsyncConnection, worker_id: int) -> int:
    ws = tables.worker_shares
    return (await conn.execute(select(func.count()).where(ws.c.worker_id == worker_id))).scalar_one()


async def set_shared(conn: AsyncConnection, worker_id: int, shared: bool) -> None:
    w = tables.workers
    await conn.execute(update(w).values(shared=shared).where(w.c.id == worker_id))
