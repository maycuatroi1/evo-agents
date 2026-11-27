"""The queries of POST /v1/worker/merged (``service.merged_work``): the worker that asks, its ended runs, and what
the hub knows merged of them."""

from __future__ import annotations

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables


async def worker_of_token(conn: AsyncConnection, token_id: int):
    """(id, revoked_at) of the worker of token ``token_id``; None when it has none."""
    w = tables.workers
    query = select(w.c.id, w.c.revoked_at).where(w.c.token_id == token_id)
    return (await conn.execute(query)).one_or_none()


async def ended(conn: AsyncConnection, worker_id: int, ids: list[int]) -> list:
    """The runs of ``ids`` worker ``worker_id`` ran that ended and that no run going on resumes."""
    r = tables.runs
    later = r.alias("later")
    going_on = (
        select(later.c.id)
        .where(later.c.resume_of_run_id == r.c.id, later.c.state.not_in(runs.TERMINAL_STATES))
        .exists()
    )
    query = select(r.c.id, r.c.project_id, r.c.kind, r.c.plan_id, r.c.repo, r.c.repos, r.c.pulls).where(
        r.c.id.in_(ids), r.c.worker_id == worker_id, r.c.state.in_(runs.TERMINAL_STATES), ~going_on
    )
    return list(await conn.execute(query))


async def plan_bodies(conn: AsyncConnection, wanted: set[tuple[int, str]]) -> list:
    """(project_id, plan_id, body) of the plans of the projects and plan ids of ``wanted``."""
    p = tables.plans
    query = select(p.c.project_id, p.c.plan_id, p.c.body).where(
        p.c.project_id.in_({project for project, _ in wanted}), p.c.plan_id.in_({plan for _, plan in wanted})
    )
    return list(await conn.execute(query))


async def merged_elsewhere(conn: AsyncConnection, ids: list[int]) -> set[int]:
    """The runs of ``ids`` that built or judged a change of the Curator the hub merged, or made or judged a merge
    request the queue merged."""
    c, q = tables.curator_changes, tables.merge_queue
    found: set[int] = set()
    for column in (c.c.builder_run_id, c.c.judge_run_id):
        found |= set((await conn.execute(select(column).where(column.in_(ids), c.c.state == "merged"))).scalars())
    for column in (q.c.run_id, q.c.judge_run_id):
        query = select(column).where(and_(column.in_(ids), q.c.merged_at.is_not(None)))
        found |= set((await conn.execute(query)).scalars())
    return found
