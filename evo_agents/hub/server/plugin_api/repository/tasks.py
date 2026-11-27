"""The queries of the tasks a plugin keeps, by their external key (``service.tasks``)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def external_key(conn: AsyncConnection, project_id: int, number: int) -> str | None:
    """The external key of task ``number`` of project ``project_id``; None for none, or a task without one."""
    t = tables.tasks.items
    query = select(t.c.external_key).where(t.c.project_id == project_id, t.c.number == number)
    return (await conn.execute(query)).scalar_one_or_none()


async def number_by_key(conn: AsyncConnection, project_id: int, stored: str) -> int | None:
    """The number of the task of project ``project_id`` under external key ``stored``, or None."""
    t = tables.tasks.items
    query = select(t.c.number).where(t.c.project_id == project_id, t.c.external_key == stored)
    return (await conn.execute(query)).scalar_one_or_none()


__all__ = ["external_key", "number_by_key"]
