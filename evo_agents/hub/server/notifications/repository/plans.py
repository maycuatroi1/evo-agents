"""The plan a run's end reports on, in SQL."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def plan_body(conn: AsyncConnection, project_id: int | None, plan_id: str | None):
    """The body of plan ``plan_id`` of project ``project_id``, or None (as for a run of no plan)."""
    pl = tables.plans
    query = select(pl.c.body).where(pl.c.project_id == project_id, pl.c.plan_id == plan_id)
    return (await conn.execute(query)).scalar_one_or_none()
