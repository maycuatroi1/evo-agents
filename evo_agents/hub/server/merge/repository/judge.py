"""The queries of the judge runs of the merge queue and of the merge checks (``service.judge``, ``service.judging``)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def login_of(conn: AsyncConnection, user_id: int) -> str:
    u = tables.users
    return (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()


async def judged_entry(conn: AsyncConnection, run_id: int, *, lock: bool = False):
    """The merge request of the queue judge run ``run_id`` judges; None for any other run."""
    q = tables.merge_queue
    query = select(q).where(q.c.judge_run_id == run_id)
    return (await conn.execute(query.with_for_update() if lock else query)).one_or_none()


async def run_request(conn: AsyncConnection, run_id: int):
    r = tables.runs
    return (await conn.execute(select(r.c.request).where(r.c.id == run_id))).scalar_one_or_none()


async def judge_run(conn: AsyncConnection, run_id: int):
    """The row of run ``run_id`` a judge run's routes read, locked; None when it is gone."""
    r = tables.runs
    return (
        await conn.execute(
            select(r.c.id, r.c.state, r.c.worker_id, r.c.kind, r.c.project_id, r.c.plan_id, r.c.model)
            .where(r.c.id == run_id)
            .with_for_update()
        )
    ).one_or_none()
