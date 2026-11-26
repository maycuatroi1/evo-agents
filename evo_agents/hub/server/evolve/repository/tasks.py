"""What an evolution reads of its project's replay tasks, probes and Curator changes."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import evolve, tables


async def select_replays(conn: AsyncConnection, project_id: int) -> list:
    """(id, split, repo, unusable, commits, base_sha) of the project's replay tasks of the select split."""
    t = tables.replay_tasks
    query = select(t.c.id, t.c.split, t.c.repo, t.c.unusable, t.c.commits, t.c.base_sha)
    return list((await conn.execute(query.where(t.c.project_id == project_id, t.c.split == evolve.SELECT))).all())


async def active_probes(conn: AsyncConnection, project_id: int) -> list:
    """(id, state, repo, arms, base_sha) of the project's active probes."""
    p = tables.curator_probes
    query = select(p.c.id, p.c.state, p.c.repo, p.c.arms, p.c.base_sha)
    return list((await conn.execute(query.where(p.c.project_id == project_id, p.c.state == "active"))).all())


async def replays_of(conn: AsyncConnection, project_id: int, ids: list[int]) -> dict:
    """id -> row of the project's replay tasks ``ids`` still of the select split."""
    t = tables.replay_tasks
    query = select(t).where(t.c.project_id == project_id, t.c.id.in_(ids), t.c.split == evolve.SELECT)
    return {row.id: row for row in (await conn.execute(query)).all()}


async def probes_of(conn: AsyncConnection, project_id: int, ids: list[int]) -> dict:
    """id -> row of the project's probes ``ids`` still active."""
    p = tables.curator_probes
    query = select(p).where(p.c.project_id == project_id, p.c.id.in_(ids), p.c.state == "active")
    return {row.id: row for row in (await conn.execute(query)).all()}


async def split_ids(conn: AsyncConnection, project_id: int, split: str, ids: list[int]) -> list[int]:
    """Those of ``ids`` that are replay tasks of the project's ``split``."""
    t = tables.replay_tasks
    query = select(t.c.id).where(t.c.project_id == project_id, t.c.split == split, t.c.id.in_(ids))
    return list((await conn.scalars(query)).all())


async def change_branch(conn: AsyncConnection, proposal_id: int) -> str | None:
    """The branch of the Curator change of proposal ``proposal_id``, or None."""
    c = tables.curator_changes
    return await conn.scalar(select(c.c.branch).where(c.c.proposal_id == proposal_id))


async def change_of(conn: AsyncConnection, project_id: int, proposal_id: int):
    """The Curator change of the project's proposal ``proposal_id``, locked; or None."""
    c = tables.curator_changes
    query = select(c).where(c.c.proposal_id == proposal_id, c.c.project_id == project_id).with_for_update()
    return (await conn.execute(query)).one_or_none()
