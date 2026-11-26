"""What reading the findings and proposals of a project and answering a proposal
(``evo_agents.hub.server.proposals.service.reads``) read and write."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.proposals.repository.record import findings_query, proposals_query


async def project_findings(conn: AsyncConnection, project_id: int, run_id: int | None, lens, limit: int) -> list:
    """The project's findings, of run ``run_id`` and lens ``lens`` when given, newest first, at most ``limit``."""
    f = tables.findings
    query = findings_query().where(f.c.project_id == project_id)
    if run_id is not None:
        query = query.where(f.c.run_id == run_id)
    if lens is not None:
        query = query.where(f.c.lens == lens)
    return list((await conn.execute(query.order_by(f.c.id.desc()).limit(limit))).all())


async def project_finding(conn: AsyncConnection, project_id: int, finding_id: int):
    f = tables.findings
    query = findings_query().where(f.c.project_id == project_id, f.c.id == finding_id)
    return (await conn.execute(query)).one_or_none()


async def project_proposals(conn: AsyncConnection, project_id: int, run_id: int | None, limit: int) -> list:
    """The project's proposals, of run ``run_id`` when given, newest first, at most ``limit``."""
    p = tables.proposals
    query = proposals_query().where(p.c.project_id == project_id)
    if run_id is not None:
        query = query.where(p.c.run_id == run_id)
    return list((await conn.execute(query.order_by(p.c.id.desc()).limit(limit))).all())


async def project_proposal(conn: AsyncConnection, project_id: int, proposal_id: int, *, lock: bool = False):
    p = tables.proposals
    query = proposals_query().where(p.c.project_id == project_id, p.c.id == proposal_id)
    if lock:
        query = query.with_for_update(of=p)
    return (await conn.execute(query)).one_or_none()


async def change_of_proposal(conn: AsyncConnection, project_id: int, proposal_id: int):
    """(id, plan_id) of the change accepting proposal ``proposal_id`` made; None without one."""
    c = tables.curator_changes
    query = select(c.c.id, c.c.plan_id).where(c.c.project_id == project_id, c.c.proposal_id == proposal_id)
    return (await conn.execute(query)).one_or_none()


async def answer(conn: AsyncConnection, proposal_id: int, user_id: int, action: str, state: str, note, days: int):
    """Record the answer ``action`` of member ``user_id`` to proposal ``proposal_id``: its new ``state``, its note, and
    for a defer the ``days`` it waits."""
    p = tables.proposals
    values = {
        "state": state,
        "answered_by": user_id,
        "answered_at": func.now(),
        "note": note,
        "deferred_until": func.now() + timedelta(days=days) if action == "defer" else None,
    }
    await conn.execute(update(p).values(**values).where(p.c.id == proposal_id))


async def read_notifications(conn: AsyncConnection, proposal_id: int) -> None:
    n = tables.notifications
    await conn.execute(
        update(n).values(read_at=func.now()).where(n.c.proposal_id == proposal_id, n.c.read_at.is_(None))
    )
