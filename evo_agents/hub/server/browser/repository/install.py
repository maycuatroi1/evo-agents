"""The columns of a worker's Install browser (workers.browser_install_*), which the list of workers and the fit of a
queued run read with ``columns``, and their writes: a request opened and ended, and the queued runs with browser."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


def columns():
    w = tables.workers
    return (
        w.c.browser_install_requested_at,
        w.c.browser_install_ended_at,
        w.c.browser_install_outcome,
        w.c.browser_install_reason,
    )


async def end(conn: AsyncConnection, worker_id: int, outcome: str, reason: str | None) -> None:
    w = tables.workers
    values = {
        "browser_install_ended_at": func.now(),
        "browser_install_outcome": outcome,
        "browser_install_reason": reason,
    }
    await conn.execute(update(w).values(**values).where(w.c.id == worker_id))


async def open_request(conn: AsyncConnection, worker_id: int) -> None:
    w = tables.workers
    values = {
        "browser_install_requested_at": func.now(),
        "browser_install_ended_at": None,
        "browser_install_outcome": None,
        "browser_install_reason": None,
    }
    await conn.execute(update(w).values(**values).where(w.c.id == worker_id))


async def open_since(conn: AsyncConnection, worker_id: int):
    """When the worker's open request was asked for; None when none is open."""
    w = tables.workers
    statement = select(w.c.browser_install_requested_at).where(
        w.c.id == worker_id,
        w.c.browser_install_requested_at.is_not(None),
        w.c.browser_install_ended_at.is_(None),
    )
    return (await conn.execute(statement)).scalar_one_or_none()


async def state(conn: AsyncConnection, worker_id: int):
    """The worker's features, browser and ``columns``."""
    w = tables.workers
    found = (w.c.features, w.c.browser, *columns())
    return (await conn.execute(select(*found).where(w.c.id == worker_id))).one()


async def locked_state(conn: AsyncConnection, worker_id: int):
    """The worker's name, owner's login, revocation, features, browser and ``columns``, its row locked; None when
    another pass holds it."""
    w, u = tables.workers, tables.users
    owner = select(u.c.login).where(u.c.id == w.c.owner_id).scalar_subquery()
    query = (
        select(w.c.name, owner.label("owner"), w.c.revoked_at, w.c.features, w.c.browser, *columns())
        .where(w.c.id == worker_id)
        .with_for_update(of=w, skip_locked=True)
    )
    return (await conn.execute(query)).one_or_none()


async def queued_with_browser(conn: AsyncConnection, batch: int) -> list[int]:
    """The oldest ``batch`` queued runs dispatched with browser."""
    r = tables.runs
    query = select(r.c.id).where(r.c.state == "queued", r.c.browser).order_by(r.c.id).limit(batch)
    return list((await conn.execute(query)).scalars().all())
