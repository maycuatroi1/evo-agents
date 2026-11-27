"""The SQL of archiving a project (``evo_agents.hub.server.project_archive``): its row held or locked, its archived
state, and its active runs."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables


async def archived_row(conn: AsyncConnection, project_id: int):
    """(name, archived_at) of project ``project_id``, or None; its row held FOR KEY SHARE until the transaction
    ends."""
    p = tables.projects
    held = select(p.c.name, p.c.archived_at).where(p.c.id == project_id).with_for_update(read=True, key_share=True)
    return (await conn.execute(held)).one_or_none()


async def lock(conn: AsyncConnection, project_id: int) -> None:
    """Lock the row of project ``project_id`` FOR UPDATE until the transaction ends."""
    p = tables.projects
    await conn.execute(select(p.c.id).where(p.c.id == project_id).with_for_update())


async def active_runs(conn: AsyncConnection, project_id: int) -> list[int]:
    """The ids of the active runs of project ``project_id`` other than the local ones, lowest first."""
    r = tables.runs
    query = select(r.c.id).where(r.c.project_id == project_id, r.c.state.in_(runs.ACTIVE_STATES), r.c.kind != "local")
    return list(await conn.scalars(query.order_by(r.c.id)))


async def archived_at(conn: AsyncConnection, project_id: int) -> datetime | None:
    p = tables.projects
    return await conn.scalar(select(p.c.archived_at).where(p.c.id == project_id))


async def set_archived(conn: AsyncConnection, project_id: int, archived_by: int | None) -> None:
    """Archive project ``project_id`` now as user ``archived_by``, or make it live again with None."""
    p = tables.projects
    if archived_by is not None:
        values = {"archived_at": func.now(), "archived_by": archived_by}
    else:
        values = {"archived_at": None, "archived_by": None}
    await conn.execute(update(p).where(p.c.id == project_id).values(**values))
