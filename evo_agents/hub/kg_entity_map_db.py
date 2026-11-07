"""The rows of the entity map runs (``kg_entity_map_runs``) and what asks for one after a build: the database half of
``evo_agents.hub.kg_entity_map_run``, kept outside the server package because ``evo_agents.hub.kg_build`` calls
``on_build`` as it records a build. A failure here never fails the build: ``on_build`` runs in a savepoint and logs
what it could not do."""

from __future__ import annotations

import logging
from collections.abc import Mapping

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import kg_entity_map_run as model
from evo_agents.hub import tables
from evo_agents.hub.tables_kg import ENTITY_MAP_ACTIVE

log = logging.getLogger(__name__)


async def active_of(conn: AsyncConnection, project_id: int):
    t = tables.kg_entity_map_runs
    query = select(t).where(t.c.project_id == project_id, t.c.state.in_(ENTITY_MAP_ACTIVE))
    return (await conn.execute(query)).one_or_none()


async def of_agent_run(conn: AsyncConnection, run_id: int):
    """The entity map run whose review run ``run_id`` is, None when none is."""
    t = tables.kg_entity_map_runs
    return (await conn.execute(select(t).where(t.c.agent_run_id == run_id).order_by(t.c.id.desc()).limit(1))).first()


async def last_revs(conn: AsyncConnection, project_id: int) -> dict | None:
    """The snapshot of the newest ended run of the project that posted a map (what ``changed`` compares with); None
    when no run did."""
    t = tables.kg_entity_map_runs
    found = await conn.scalar(
        select(t.c.revs)
        .where(t.c.project_id == project_id, t.c.state == "done", t.c.maps > 0, t.c.revs.is_not(None))
        .order_by(t.c.id.desc())
        .limit(1)
    )
    return found if isinstance(found, dict) else None


async def posted(conn: AsyncConnection, run_id: int) -> int | None:
    """Count one map posted by review run ``run_id`` on its entity map run; that run's id, None when it is none's."""
    t = tables.kg_entity_map_runs
    return await conn.scalar(
        update(t).values(maps=t.c.maps + 1, updated_at=func.now()).where(t.c.agent_run_id == run_id).returning(t.c.id)
    )


async def create(
    conn: AsyncConnection,
    project_id: int,
    via: str,
    why: list[str],
    *,
    requested_by: int | None = None,
    build_id: int | None = None,
):
    """Queue a run of ``project_id``: (the row, True), or (the run in flight, False): one runs at a time."""
    t = tables.kg_entity_map_runs
    values = {
        "project_id": project_id,
        "via": via,
        "requested_by": requested_by,
        "state": "queued",
        "why": why,
        "build_id": build_id,
        "body": {},
    }
    try:
        async with conn.begin_nested():
            return (await conn.execute(insert(t).values(**values).returning(t))).one(), True
    except IntegrityError:  # another call queued one in between
        found = await active_of(conn, project_id)
        if found is None:
            raise
        return found, False


async def on_build(conn: AsyncConnection, project_id: int, build_id: int, coverage: Mapping | None) -> int | None:
    """Queue an entity map run after build ``build_id`` of the project succeeded with ``coverage``, when
    ``model.wants_run`` says so: the first build under strategy entity-graph-agent, or a map whose citations went stale
    or whose citable items went uncited. Its id, or None."""
    if not isinstance(((coverage or {}).get("entity_map")), Mapping):
        return None
    t = tables.kg_entity_map_runs
    last = (
        await conn.execute(select(t.c.created_at).where(t.c.project_id == project_id).order_by(t.c.id.desc()).limit(1))
    ).first()
    now = (await conn.execute(select(func.now()))).scalar_one()
    wanted = model.wants_run(coverage, had_run=last is not None, last_asked=None if last is None else last[0], now=now)
    if wanted is None or await active_of(conn, project_id) is not None:
        return None
    via, why = wanted
    row, made = await create(conn, project_id, via, why, build_id=build_id)
    log.info("kg entity map run queued", extra={"project_id": project_id, "id": row.id, "via": via})
    return row.id if made else None
