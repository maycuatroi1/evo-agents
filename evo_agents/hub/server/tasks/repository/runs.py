"""The queries of who a run of the hub acts for, which the tool ``tasks__create`` reads: a member by login
(``user_id``), the kind and branch of a run (``run``), and who approved what a split run of @ba splits
(``approver_of``).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def user_id(conn: AsyncConnection, login: str) -> int | None:
    u = tables.users
    return (await conn.execute(select(u.c.id).where(u.c.login == login))).scalar_one_or_none()


async def run(conn: AsyncConnection, run_id: int):
    """(kind, branch) of run ``run_id``, or None."""
    r = tables.runs
    return (await conn.execute(select(r.c.kind, r.c.branch).where(r.c.id == run_id))).first()


async def run_kind(conn: AsyncConnection, run_id: int) -> str:
    r = tables.runs
    return (await conn.execute(select(r.c.kind).where(r.c.id == run_id))).scalar_one()


async def approver_of(conn: AsyncConnection, split_run_id: int) -> int | None:
    """Who approved the SRS version whose frame tasks run ``split_run_id`` splits, else who released the use case
    whose ``tasks`` keeps that run; None when neither does."""
    a, e = tables.srs.approval_tasks, tables.srs.use_case_releases
    approver = await conn.scalar(select(a.c.approved_by).where(a.c.split_run_id == split_run_id))
    if approver is None:
        released = select(e.c.released_by).where(e.c.tasks["split_run_id"].astext == str(split_run_id)).limit(1)
        approver = await conn.scalar(released)
    return approver


__all__ = ["approver_of", "run", "run_kind", "user_id"]
