"""The holds of browser identities by runs (``browser_identities.held_by_run``): the identities a run was dispatched
with, taken while free and live, and given back."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def release(conn: AsyncConnection, run_id: int) -> None:
    """Take back every identity run ``run_id`` holds."""
    b = tables.browser_identities
    await conn.execute(update(b).values(held_by_run=None).where(b.c.held_by_run == run_id))


async def dispatched_with(conn: AsyncConnection, run_id: int) -> list[int] | None:
    """The identities run ``run_id`` was dispatched with; None for no such run."""
    r = tables.runs
    return (await conn.execute(select(r.c.browser_identities).where(r.c.id == run_id))).scalar_one_or_none()


async def hold(conn: AsyncConnection, run_id: int, ids: list[int]) -> None:
    """Give run ``run_id`` the identities of ``ids`` that are still free and live."""
    b = tables.browser_identities
    free = (b.c.id.in_(ids), b.c.held_by_run.is_(None), b.c.deleted_at.is_(None))
    await conn.execute(update(b).values(held_by_run=run_id, last_used_at=func.now()).where(*free))
