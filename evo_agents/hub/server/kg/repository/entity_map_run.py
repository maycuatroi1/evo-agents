"""The SQL of the runs that map a project's entities to its code (``evo_agents.hub.server.kg_entity_map_run``): the
rows of kg_entity_map_runs with their project, the latest build and the state of the review run.
``evo_agents.hub.kg_entity_map_db`` holds the rows the map's route and the job share."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.tables_kg import ENTITY_MAP_ACTIVE


async def now(conn: AsyncConnection) -> datetime:
    return (await conn.execute(select(func.now()))).scalar_one()


async def set_run(conn: AsyncConnection, run_id: int, **values) -> None:
    t = tables.kg_entity_map_runs
    await conn.execute(update(t).where(t.c.id == run_id).values(**values, updated_at=func.now()))


def context():
    """The runs with their project's name."""
    t, p = tables.kg_entity_map_runs, tables.projects
    return select(t, p.c.name.label("project")).join_from(t, p, p.c.id == t.c.project_id)


async def active_contexts(conn: AsyncConnection) -> Sequence[Any]:
    t = tables.kg_entity_map_runs
    return (await conn.execute(context().where(t.c.state.in_(ENTITY_MAP_ACTIVE)).order_by(t.c.id))).all()


async def latest_build(conn: AsyncConnection, project_id: int):
    b = tables.kg_builds
    query = (
        select(b.c.id, b.c.coverage)
        .where(b.c.project_id == project_id, b.c.status == "succeeded")
        .order_by(b.c.id.desc())
        .limit(1)
    )
    return (await conn.execute(query)).first()


async def run_state(conn: AsyncConnection, run_id: int) -> str | None:
    r = tables.runs
    return await conn.scalar(select(r.c.state).where(r.c.id == run_id))


async def maps_of(conn: AsyncConnection, entity_map_run_id: int) -> int:
    """The maps the review run of the entity map run posted."""
    t = tables.kg_entity_map_runs
    return int(await conn.scalar(select(t.c.maps).where(t.c.id == entity_map_run_id)) or 0)


async def runs_of(conn: AsyncConnection, project_id: int, limit: int) -> Sequence[Any]:
    t = tables.kg_entity_map_runs
    return (
        await conn.execute(select(t).where(t.c.project_id == project_id).order_by(t.c.id.desc()).limit(limit))
    ).all()
