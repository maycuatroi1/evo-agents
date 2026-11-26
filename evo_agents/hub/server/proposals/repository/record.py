"""What a review run records (``evo_agents.hub.server.proposals.service.record``) reads and writes: the run and its
owner, the evidence it cites, its findings and proposals, the charter, and the night's figures."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables, tiers


async def review_run_row(conn: AsyncConnection, run_id: int):
    """(state, worker_id, kind, project_id, name, dispatched_by, login) of run ``run_id``, locked; None without it."""
    r, p, u = tables.runs, tables.projects, tables.users
    query = (
        select(r.c.state, r.c.worker_id, r.c.kind, r.c.project_id, p.c.name, r.c.dispatched_by, u.c.login)
        .join_from(r, p, p.c.id == r.c.project_id)
        .join(u, u.c.id == r.c.dispatched_by)
        .where(r.c.id == run_id)
        .with_for_update(of=r)
    )
    return (await conn.execute(query)).one_or_none()


async def local_run_row(conn: AsyncConnection, run_id: int):
    """(state, kind, dispatched_by, project_id, name) of run ``run_id``, locked; None without it."""
    r, p = tables.runs, tables.projects
    query = (
        select(r.c.state, r.c.kind, r.c.dispatched_by, r.c.project_id, p.c.name)
        .join_from(r, p, p.c.id == r.c.project_id)
        .where(r.c.id == run_id)
        .with_for_update(of=r)
    )
    return (await conn.execute(query)).one_or_none()


async def project_repos(conn: AsyncConnection, project_id: int) -> set[str]:
    pr = tables.project_repos
    return set((await conn.execute(select(pr.c.name).where(pr.c.project_id == project_id))).scalars())


async def cited_run(conn: AsyncConnection, run_id: int, project_id: int):
    """(event_seq, kind, plan_id, dispatched_by) of run ``run_id`` of the project; None without it."""
    r = tables.runs
    query = select(r.c.event_seq, r.c.kind, r.c.plan_id, r.c.dispatched_by).where(
        r.c.id == run_id, r.c.project_id == project_id
    )
    return (await conn.execute(query)).one_or_none()


def findings_query():
    f = tables.findings
    return select(
        f.c.id,
        f.c.run_id,
        f.c.lens,
        f.c.severity,
        f.c.title,
        f.c.body,
        f.c.evidence,
        f.c.created_at,
        f.c.fix_of,
        f.c.label,
    )


async def findings_of_run(conn: AsyncConnection, run_id: int) -> int:
    f = tables.findings
    return (await conn.execute(select(func.count()).where(f.c.run_id == run_id))).scalar_one()


async def insert_finding(conn: AsyncConnection, values: dict) -> int:
    f = tables.findings
    return (await conn.execute(insert(f).values(**values).returning(f.c.id))).scalar_one()


async def finding_row(conn: AsyncConnection, finding_id: int):
    f = tables.findings
    return (await conn.execute(findings_query().where(f.c.id == finding_id))).one()


async def charter_body(conn: AsyncConnection, project_id: int) -> dict:
    c = tables.charters
    query = select(c.c.body).where(c.c.project_id == project_id).order_by(c.c.revision.desc()).limit(1)
    return (await conn.execute(query)).scalar_one_or_none() or {}


async def finding_pieces(conn: AsyncConnection, project_id: int, ids: list[int]) -> list:
    """(id, label, pieces) of findings ``ids`` of the project: how many pieces of evidence each holds."""
    f = tables.findings
    return list(
        (
            await conn.execute(
                select(f.c.id, f.c.label, func.jsonb_array_length(f.c.evidence).label("pieces")).where(
                    f.c.project_id == project_id, f.c.id.in_(ids)
                )
            )
        ).all()
    )


async def rejected_like(conn: AsyncConnection, project_id: int, fingerprint: str):
    p = tables.proposals
    query = (
        select(p.c.id, p.c.evidence_count)
        .where(
            p.c.project_id == project_id,
            p.c.fingerprint == fingerprint,
            p.c.state == "rejected",
            p.c.answered_at >= func.now() - timedelta(days=tiers.REJECTED_DAYS),
        )
        .order_by(p.c.answered_at.desc())
        .limit(1)
    )
    return (await conn.execute(query)).one_or_none()


def proposals_query():
    p, u = tables.proposals, tables.users.alias("answerer")
    return select(
        p.c.id,
        p.c.run_id,
        p.c.lens,
        p.c.kind,
        p.c.title,
        p.c.tier,
        p.c.state,
        p.c.evidence_count,
        p.c.duplicate_of,
        u.c.login.label("answered_by"),
        p.c.answered_at,
        p.c.deferred_until,
        p.c.inbox_at,
        p.c.created_at,
        p.c.summary,
        p.c.paths,
        p.c.impacted,
        p.c.tier_reasons,
        p.c.finding_ids,
        p.c.evidence,
        p.c.draft.label("plan"),
        p.c.note,
        p.c.label,
        p.c.revert_of,
        p.c.fix_of,
        p.c.promotion,
        p.c.system_change,
    ).select_from(p.outerjoin(u, u.c.id == p.c.answered_by))


async def proposals_of_run(conn: AsyncConnection, run_id: int) -> int:
    p = tables.proposals
    return (await conn.execute(select(func.count()).where(p.c.run_id == run_id))).scalar_one()


async def insert_proposal(conn: AsyncConnection, values: dict) -> int:
    p = tables.proposals
    return (await conn.execute(insert(p).values(**values).returning(p.c.id))).scalar_one()


async def proposal_row(conn: AsyncConnection, proposal_id: int):
    p = tables.proposals
    return (await conn.execute(proposals_query().where(p.c.id == proposal_id))).one()


async def night_figures(conn: AsyncConnection, project_id: int, night):
    """(night, since, until, run_id, figures, created_at) of the project's latest night, or ``night``'s; None
    without one."""
    cf = tables.curator_figures
    query = select(cf.c.night, cf.c.since, cf.c.until, cf.c.run_id, cf.c.figures, cf.c.created_at).where(
        cf.c.project_id == project_id
    )
    if night is not None:
        query = query.where(cf.c.night == night)
    return (await conn.execute(query.order_by(cf.c.night.desc()).limit(1))).one_or_none()
