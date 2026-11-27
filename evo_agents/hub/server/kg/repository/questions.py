"""The SQL of the starter questions of a kg_init run (``evo_agents.hub.server.kg.service.questions``): the run's
body, the project's eval set and the state of the questions run."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def save(conn: AsyncConnection, init_id: int, body: dict, **values) -> None:
    """The body of kg_init run ``init_id``, with ``values`` of its other columns."""
    t = tables.kg_init_runs
    await conn.execute(update(t).where(t.c.id == init_id).values(body=body, updated_at=func.now(), **values))


async def latest_set(conn: AsyncConnection, project_id: int):
    """(name, n questions, created_by) of the latest version of the project's eval set; None without one."""
    s = tables.kg_eval_sets
    return (
        await conn.execute(
            select(s.c.name, func.jsonb_array_length(s.c.questions).label("n"), s.c.created_by)
            .where(s.c.project_id == project_id)
            .order_by(s.c.version.desc())
            .limit(1)
        )
    ).first()


async def latest_version(conn: AsyncConnection, project_id: int) -> int | None:
    """The latest version of the project's eval set; None without one."""
    s = tables.kg_eval_sets
    return await conn.scalar(
        select(s.c.version).where(s.c.project_id == project_id).order_by(s.c.version.desc()).limit(1)
    )


async def run_state(conn: AsyncConnection, run_id: int | None) -> str | None:
    r = tables.runs
    return await conn.scalar(select(r.c.state).where(r.c.id == run_id))


async def init_run(conn: AsyncConnection, project_id: int, run_id: int):
    """kg_init run ``run_id`` of the project, locked; None without it."""
    t = tables.kg_init_runs
    return (
        await conn.execute(select(t).where(t.c.id == run_id, t.c.project_id == project_id).with_for_update())
    ).first()


async def lock_project(conn: AsyncConnection, project_id: int) -> None:
    await conn.execute(select(tables.projects.c.id).where(tables.projects.c.id == project_id).with_for_update())


async def add_set(
    conn: AsyncConnection,
    project_id: int,
    version: int,
    name: str,
    entries: list[dict],
    digest: str,
    created_by: int | None,
) -> None:
    s = tables.kg_eval_sets
    await conn.execute(
        s.insert().values(
            project_id=project_id,
            version=version,
            name=name,
            questions=entries,
            digest=digest,
            created_by=created_by,
        )
    )
