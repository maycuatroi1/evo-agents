"""The queries of the merge checks (merge_checks, schema 0187), and of the runs and the waits they read."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.merge.models.check import FAILED, JUDGING, PASSED


async def row_of(conn: AsyncConnection, check_id: int, *, lock: bool = False):
    m = tables.merge_checks
    query = select(m).where(m.c.id == check_id)
    return (await conn.execute(query.with_for_update() if lock else query)).one_or_none()


async def fresh_row(conn: AsyncConnection, check_id: int):
    """Check ``check_id`` as it is now, one the caller knows is there."""
    m = tables.merge_checks
    return (await conn.execute(select(m).where(m.c.id == check_id))).one()


async def judged_check(conn: AsyncConnection, run_id: int, *, lock: bool = False):
    """The merge check judge run ``run_id`` judges; None for any other run."""
    m = tables.merge_checks
    query = select(m).where(m.c.judge_run_id == run_id)
    return (await conn.execute(query.with_for_update() if lock else query)).one_or_none()


async def set_row(conn: AsyncConnection, check_id: int, **values) -> None:
    m = tables.merge_checks
    await conn.execute(update(m).values(**values, updated_at=func.now()).where(m.c.id == check_id))


async def of_pull(conn: AsyncConnection, project_id: int, repo: str, number: int, *, lock: bool = False):
    """The check of pull request ``number`` of ``repo`` in project ``project_id``; None when the run never asked."""
    m = tables.merge_checks
    query = select(m).where(m.c.project_id == project_id, m.c.repo == repo, m.c.number == number)
    return (await conn.execute(query.with_for_update() if lock else query)).one_or_none()


async def insert_check(conn: AsyncConnection, values: dict) -> int:
    m = tables.merge_checks
    return (await conn.execute(insert(m).values(values).returning(m.c.id))).scalar_one()


async def judging_ids(conn: AsyncConnection) -> list[int]:
    """The ids of the checks that judge, oldest first."""
    m = tables.merge_checks
    return list((await conn.execute(select(m.c.id).where(m.c.state == JUDGING).order_by(m.c.id))).scalars().all())


async def passed_since(conn: AsyncConnection, since: datetime) -> list:
    """The checks that passed, made at or after ``since``, whose merge is not recorded yet."""
    m = tables.merge_checks
    query = select(m).where(m.c.state == PASSED, m.c.created_at >= since).order_by(m.c.id)
    return [row for row in await conn.execute(query) if "merged" not in (row.verdict or {})]


async def settled_since(conn: AsyncConnection, since: datetime) -> list:
    """The checks that passed or failed, last changed at or after ``since``."""
    m = tables.merge_checks
    query = select(m).where(m.c.state.in_((PASSED, FAILED)), m.c.updated_at >= since).order_by(m.c.id)
    return list(await conn.execute(query))


# The runs and the waits of the checks


async def run_model(conn: AsyncConnection, run_id: int):
    r = tables.runs
    return (await conn.execute(select(r.c.model).where(r.c.id == run_id))).scalar_one_or_none()


async def run_project(conn: AsyncConnection, run_id: int) -> int:
    r = tables.runs
    return (await conn.execute(select(r.c.project_id).where(r.c.id == run_id))).scalar_one()


async def run_end(conn: AsyncConnection, run_id: int):
    """(state, failure_cause, error) of run ``run_id``; None when it is gone."""
    r = tables.runs
    query = select(r.c.state, r.c.failure_cause, r.c.error).where(r.c.id == run_id)
    return (await conn.execute(query)).one_or_none()


async def wait_of(conn: AsyncConnection, decision_id: int):
    """(run_id, state, wake_at) of decision ``decision_id``; None when it is gone."""
    d = tables.decisions
    query = select(d.c.run_id, d.c.state, d.c.wake_at).where(d.c.id == decision_id)
    return (await conn.execute(query)).one_or_none()


async def decision_plan(conn: AsyncConnection, decision_id: int):
    d = tables.decisions
    return (await conn.execute(select(d.c.plan_id).where(d.c.id == decision_id))).scalar_one_or_none()
