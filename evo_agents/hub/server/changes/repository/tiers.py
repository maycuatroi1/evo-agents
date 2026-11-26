"""What the tier of a change of the Curator (``evo_agents.hub.server.changes.service.tiers``) reads and writes: the
owner a graph is read as, the latest measure of the graph's health, the proposal's kind and tier, the charter, and the
change's replays."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.changes.repository.job import NIGHT_SHIFT


def _night_owner(project_id: int):
    s, p, u = tables.schedules, tables.projects, tables.users
    return (
        select(p.c.name.label("project"), s.c.owner_id, u.c.login.label("owner"))
        .join_from(s, p, p.c.id == s.c.project_id)
        .join(u, u.c.id == s.c.owner_id)
        .where(s.c.project_id == project_id, s.c.kind == NIGHT_SHIFT)
        .order_by(s.c.id)
        .limit(1)
    )


def _member(project_id: int, user_id: int):
    p, u = tables.projects, tables.users
    found = select(p.c.name.label("project"), u.c.id.label("owner_id"), u.c.login.label("owner"))
    return found.where(p.c.id == project_id, u.c.id == user_id)


async def graph_owner(conn: AsyncConnection, project_id: int, owner_id: int | None):
    """(project, owner_id, owner) of the owner of the project's night shift, or of member ``owner_id``; None without
    one."""
    query = _night_owner(project_id) if owner_id is None else _member(project_id, owner_id)
    return (await conn.execute(query)).one_or_none()


async def latest_measure(conn: AsyncConnection, project_id: int):
    """(measured_at, body) of the latest successful measure of the project's graph health, or None."""
    h = tables.kg_health
    return (
        await conn.execute(
            select(h.c.measured_at, h.c.body)
            .where(h.c.project_id == project_id, h.c.error.is_(None))
            .order_by(h.c.measured_at.desc(), h.c.id.desc())
            .limit(1)
        )
    ).one_or_none()


async def proposal_kind(conn: AsyncConnection, proposal_id: int) -> str:
    pr = tables.proposals
    return (await conn.execute(select(pr.c.kind).where(pr.c.id == proposal_id))).scalar_one()


async def charter_body(conn: AsyncConnection, project_id: int) -> dict:
    """The body of the project's latest charter, or {}."""
    c = tables.charters
    query = select(c.c.body).where(c.c.project_id == project_id).order_by(c.c.revision.desc()).limit(1)
    return (await conn.execute(query)).scalar_one_or_none() or {}


async def locked_tier(conn: AsyncConnection, proposal_id: int):
    """(kind, tier, tier_reasons, fix_of) of proposal ``proposal_id``, locked for update."""
    pr = tables.proposals
    return (
        await conn.execute(
            select(pr.c.kind, pr.c.tier, pr.c.tier_reasons, pr.c.fix_of).where(pr.c.id == proposal_id).with_for_update()
        )
    ).one()


async def raise_proposal(conn: AsyncConnection, proposal_id: int, tier: int, reasons: list) -> None:
    pr = tables.proposals
    await conn.execute(update(pr).values(tier=tier, tier_reasons=reasons).where(pr.c.id == proposal_id))


async def replay_rows(conn: AsyncConnection, change_id: int) -> list[dict]:
    """(split, state, result_a, result_b) of change ``change_id``'s replays, in order."""
    rp, t = tables.curator_replays, tables.replay_tasks
    query = (
        select(t.c.split, rp.c.state, rp.c.result_a, rp.c.result_b)
        .join_from(rp, t, t.c.id == rp.c.task_id)
        .where(rp.c.change_id == change_id)
        .order_by(rp.c.id)
    )
    return [dict(row._mapping) for row in await conn.execute(query)]


async def merge_proposal(conn: AsyncConnection, proposal_id: int):
    """(kind, tier, fix_of, paths) of proposal ``proposal_id``."""
    pr = tables.proposals
    query = select(pr.c.kind, pr.c.tier, pr.c.fix_of, pr.c.paths).where(pr.c.id == proposal_id)
    return (await conn.execute(query)).one()
