"""The rows of curator_evolve_runs, and what an evolution reads of its project, its run and its schedule."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def listed(conn: AsyncConnection, project_id: int, state: str | None, limit: int) -> list:
    """The project's evolutions, of ``state`` alone when it is given, the newest first."""
    e = tables.curator_evolve_runs
    query = select(e).where(e.c.project_id == project_id)
    if state is not None:
        query = query.where(e.c.state == state)
    return list((await conn.execute(query.order_by(e.c.id.desc()).limit(limit))).all())


async def of_project(conn: AsyncConnection, project_id: int, evolve_id: int):
    """Evolution ``evolve_id`` of the project, or None."""
    e = tables.curator_evolve_runs
    query = select(e).where(e.c.id == evolve_id, e.c.project_id == project_id)
    return (await conn.execute(query)).one_or_none()


async def one(conn: AsyncConnection, evolve_id: int, *, lock: bool = False):
    """Evolution ``evolve_id``, which is there; locked with ``lock``."""
    e = tables.curator_evolve_runs
    query = select(e).where(e.c.id == evolve_id)
    return (await conn.execute(query.with_for_update() if lock else query)).one()


async def inserted(conn: AsyncConnection, values: dict) -> int:
    e = tables.curator_evolve_runs
    return (await conn.execute(pg_insert(e).values(**values).returning(e.c.id))).scalar_one()


async def set_row(conn: AsyncConnection, evolve_id: int, **values) -> None:
    e = tables.curator_evolve_runs
    await conn.execute(update(e).values(**values, updated_at=func.now()).where(e.c.id == evolve_id))


async def set_iterations(conn: AsyncConnection, evolve_id: int, iterations: int) -> None:
    e = tables.curator_evolve_runs
    await conn.execute(update(e).values(iterations=iterations).where(e.c.id == evolve_id))


async def set_frontier(conn: AsyncConnection, evolve_id: int, frontier: list[dict]) -> None:
    e = tables.curator_evolve_runs
    await conn.execute(update(e).values(frontier=frontier, updated_at=func.now()).where(e.c.id == evolve_id))


async def oldest_queued(conn: AsyncConnection, project_id: int):
    """The oldest queued evolution of the project, locked, one another pass holds skipped; or None."""
    e = tables.curator_evolve_runs
    query = select(e).where(e.c.project_id == project_id, e.c.state == "queued").order_by(e.c.id).limit(1)
    return (await conn.execute(query.with_for_update(skip_locked=True))).one_or_none()


async def running_of_run(conn: AsyncConnection, run_id: int):
    """(id, target) of the running evolution of run ``run_id``, locked; or None."""
    e = tables.curator_evolve_runs
    query = select(e.c.id, e.c.target).where(e.c.run_id == run_id, e.c.state == "running").with_for_update()
    return (await conn.execute(query)).one_or_none()


async def target_of_run(conn: AsyncConnection, run_id: int):
    """(id, target) of the evolution of run ``run_id``, or None."""
    e = tables.curator_evolve_runs
    return (await conn.execute(select(e.c.id, e.c.target).where(e.c.run_id == run_id))).one_or_none()


async def state_of_run(conn: AsyncConnection, run_id: int):
    """(id, state) of the evolution of run ``run_id``, locked; or None."""
    e = tables.curator_evolve_runs
    query = select(e.c.id, e.c.state).where(e.c.run_id == run_id).with_for_update()
    return (await conn.execute(query)).one_or_none()


async def of_run(conn: AsyncConnection, run_id: int):
    """The evolution of run ``run_id``, locked; or None."""
    e = tables.curator_evolve_runs
    return (await conn.execute(select(e).where(e.c.run_id == run_id).with_for_update())).one_or_none()


async def held_run(conn: AsyncConnection, run_id: int):
    """(state, worker_id, kind) of run ``run_id``, locked; or None."""
    r = tables.runs
    query = select(r.c.state, r.c.worker_id, r.c.kind).where(r.c.id == run_id).with_for_update()
    return (await conn.execute(query)).one_or_none()


async def project_name(conn: AsyncConnection, project_id: int) -> str:
    p = tables.projects
    return (await conn.execute(select(p.c.name).where(p.c.id == project_id))).scalar_one()


async def project_name_or_none(conn: AsyncConnection, project_id: int) -> str | None:
    p = tables.projects
    return await conn.scalar(select(p.c.name).where(p.c.id == project_id))


async def has_repo(conn: AsyncConnection, project_id: int, repo: str | None) -> bool:
    pr = tables.project_repos
    query = select(pr.c.name).where(pr.c.project_id == project_id, pr.c.name == repo)
    return await conn.scalar(query) is not None


async def has_proposal(conn: AsyncConnection, project_id: int, proposal_id: int) -> bool:
    p = tables.proposals
    query = select(p.c.id).where(p.c.id == proposal_id, p.c.project_id == project_id)
    return await conn.scalar(query) is not None


async def due_schedule(conn: AsyncConnection, schedules, project_id: int):
    """The project's row of ``schedules``, the night shift's schedules as ``curator_shift.due_schedules`` selects
    them; or None."""
    return (await conn.execute(schedules.where(tables.schedules.c.project_id == project_id))).one_or_none()
