"""The queries of the cron jobs of a project beyond ``evo_agents.hub.cron_store``: what the runs of their fires
cost, what a job's work names, and the runs a fire started."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server.night_cost import usage_cost


async def costs(conn: AsyncConnection, job_ids: list[int], since: datetime) -> dict[int, float]:
    """job id -> what the runs of its fires queued since ``since`` cost."""
    r = tables.runs
    query = (
        select(r.c.cron_job_id, func.coalesce(func.sum(usage_cost(r.c.usage)), 0).label("cost"))
        .where(and_(r.c.cron_job_id.in_(job_ids), r.c.queued_at >= since))
        .group_by(r.c.cron_job_id)
    )
    return {row.cron_job_id: float(row.cost or 0) for row in await conn.execute(query)}


async def agent_id(conn: AsyncConnection, project_id: int, handle: str) -> int | None:
    """The id of the live agent profile ``handle`` of the project; None when it has none."""
    ap = tables.agent_profiles
    query = select(ap.c.id).where(ap.c.project_id == project_id, ap.c.handle == handle, ap.c.deleted_at.is_(None))
    return (await conn.execute(query)).scalar_one_or_none()


async def in_chat(conn: AsyncConnection, chat_id: int, user_id: int) -> bool:
    m = tables.chat_members
    query = select(m.c.chat_id).where(m.c.chat_id == chat_id, m.c.user_id == user_id, m.c.removed_at.is_(None))
    return (await conn.execute(query)).first() is not None


async def has_plan(conn: AsyncConnection, project_id: int, plan_id: str) -> bool:
    p = tables.plans
    query = select(p.c.plan_id).where(p.c.project_id == project_id, p.c.plan_id == plan_id)
    return (await conn.execute(query)).first() is not None


async def has_repo(conn: AsyncConnection, project_id: int, name: str) -> bool:
    pr = tables.project_repos
    query = select(pr.c.name).where(pr.c.project_id == project_id, pr.c.name == name)
    return (await conn.execute(query)).first() is not None


async def active_run(conn: AsyncConnection, job_id: int):
    """(id, state) of the first run of the job's fires that has not ended; None when there is none."""
    r = tables.runs
    query = (
        select(r.c.id, r.c.state)
        .where(r.c.cron_job_id == job_id, r.c.state.in_(runs.ACTIVE_STATES))
        .order_by(r.c.id)
        .limit(1)
    )
    return (await conn.execute(query)).one_or_none()


async def mark_run(conn: AsyncConnection, run_id: int, job_id: int) -> None:
    """Name the job whose fire started run ``run_id``."""
    r = tables.runs
    await conn.execute(r.update().where(r.c.id == run_id).values(cron_job_id=job_id))


async def ended_runs(conn: AsyncConnection, job_id: int, since: datetime) -> list:
    """(id, state, failure_cause) of the job's latest 50 runs queued since ``since`` that ended done or failed, the
    latest first."""
    r = tables.runs
    query = (
        select(r.c.id, r.c.state, r.c.failure_cause)
        .where(r.c.cron_job_id == job_id, r.c.state.in_(("done", "failed")), r.c.queued_at >= since)
        .order_by(r.c.finished_at.desc().nulls_last(), r.c.id.desc())
        .limit(50)
    )
    return list(await conn.execute(query))
