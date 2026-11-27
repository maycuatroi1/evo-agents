"""The queries of what the worker tells the hub of a run's autonomy (``autonomy.service.worker``)."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def project_id(conn: AsyncConnection, run_id: int) -> int:
    r = tables.runs
    return (await conn.execute(select(r.c.project_id).where(r.c.id == run_id))).scalar_one()


async def decision(conn: AsyncConnection, decision_id: int, run_id: int, categories: Iterable[str]):
    """(state, approval) of decision ``decision_id`` of run ``run_id`` in one of ``categories``; None otherwise."""
    d = tables.decisions
    query = select(d.c.state, d.c.approval).where(
        d.c.id == decision_id, d.c.run_id == run_id, d.c.category.in_(tuple(categories))
    )
    return (await conn.execute(query)).one_or_none()
