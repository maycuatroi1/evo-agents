"""The SQL of what earlier review nights found (``evo_agents.hub.server.earlier``): a project's findings, the
proposals that name them with their changes and ledgers, and the proposals rejected lately."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables, tiers
from evo_agents.hub.server.runs.models.earlier import SCAN


async def findings(conn: AsyncConnection, project_id: int, lenses: list[str] | None) -> list:
    """The SCAN newest findings of the project, of ``lenses`` when given, with the night of their run."""
    f, r = tables.findings, tables.runs
    query = (
        select(
            f.c.id,
            f.c.run_id,
            f.c.lens,
            f.c.severity,
            f.c.title,
            f.c.evidence,
            f.c.label,
            f.c.created_at,
            r.c.schedule_night,
        )
        .join_from(f, r, r.c.id == f.c.run_id)
        .where(f.c.project_id == project_id)
    )
    if lenses is not None:
        query = query.where(f.c.lens.in_(lenses))
    return list((await conn.execute(query.order_by(f.c.id.desc()).limit(SCAN))).all())


async def proposals_naming(conn: AsyncConnection, project_id: int, ids: list[int]) -> list:
    """The proposals of the project that name any of findings ``ids``, oldest first, with their change's state, its
    merge and the ledger's latest outcome of it."""
    p, c, lg = tables.proposals, tables.curator_changes, tables.curator_ledger
    outcome = (
        select(lg.c.outcome)
        .where(lg.c.change_id == c.c.id, lg.c.action == "outcome")
        .order_by(lg.c.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    query = (
        select(
            p.c.id,
            p.c.kind,
            p.c.tier,
            p.c.state,
            p.c.note,
            p.c.finding_ids,
            p.c.label,
            c.c.state.label("change_state"),
            c.c.merged_at,
            outcome.label("outcome"),
        )
        .select_from(p.outerjoin(c, c.c.proposal_id == p.c.id))
        .where(p.c.project_id == project_id, p.c.finding_ids.overlap(ids))
        .order_by(p.c.id)
    )
    return list((await conn.execute(query)).all())


async def rejected_lately(conn: AsyncConnection, project_id: int) -> list:
    """The SCAN newest proposals of the project rejected in the last REJECTED_DAYS days."""
    p = tables.proposals
    query = (
        select(p.c.id, p.c.lens, p.c.kind, p.c.title, p.c.note, p.c.answered_at, p.c.label)
        .where(
            p.c.project_id == project_id,
            p.c.state == "rejected",
            p.c.answered_at >= func.now() - timedelta(days=tiers.REJECTED_DAYS),
        )
        .order_by(p.c.answered_at.desc(), p.c.id.desc())
        .limit(SCAN)
    )
    return list((await conn.execute(query)).all())
