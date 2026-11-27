"""The SQL of the Curator's ledger: the one INSERT of curator_ledger, and what its lines read of proposals, changes,
findings, the night's figures and the runs."""

from __future__ import annotations

from sqlalchemy import exists, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def insert_line(conn: AsyncConnection, values: dict) -> int:
    """The one write of curator_ledger, a line of ``values``; its id."""
    lg = tables.curator_ledger
    return (await conn.execute(insert(lg).values(**values).returning(lg.c.id))).scalar_one()


async def finding_evidence(conn: AsyncConnection, finding_ids: list[int]) -> list:
    """The ``evidence`` of each finding of ``finding_ids``."""
    f = tables.findings
    return list((await conn.execute(select(f.c.evidence).where(f.c.id.in_(finding_ids)))).scalars())


async def run_figures(conn: AsyncConnection, run_id: int):
    """The row (``figures``, ``night``) of the night's figures review run ``run_id`` was queued on, or None."""
    cf, r = tables.curator_figures, tables.runs
    row = (
        await conn.execute(select(cf.c.figures, cf.c.night).where(cf.c.run_id == run_id).order_by(cf.c.id).limit(1))
    ).one_or_none()
    if row is None:  # a run whose figures were counted, then queued again: the night's figures all the same
        run = (await conn.execute(select(r.c.project_id, r.c.schedule_night).where(r.c.id == run_id))).one()
        row = (
            await conn.execute(
                select(cf.c.figures, cf.c.night).where(
                    cf.c.project_id == run.project_id, cf.c.night == run.schedule_night
                )
            )
        ).one_or_none()
    return row


async def recorded_proposal(conn: AsyncConnection, proposal_id: int):
    """What the first line of proposal ``proposal_id`` reads of it."""
    p = tables.proposals
    return (
        await conn.execute(
            select(
                p.c.id,
                p.c.run_id,
                p.c.lens,
                p.c.kind,
                p.c.tier,
                p.c.title,
                p.c.state,
                p.c.duplicate_of,
                p.c.evidence,
                p.c.finding_ids,
                p.c.evidence_count,
            ).where(p.c.id == proposal_id)
        )
    ).one()


async def change(conn: AsyncConnection, change_id: int):
    """The row of curator_changes ``change_id``."""
    c = tables.curator_changes
    return (await conn.execute(select(c).where(c.c.id == change_id))).one()


async def builder_commit(conn: AsyncConnection, run_id: int) -> str | None:
    """The commit the Builder's last step report named, else the run's own."""
    e, r = tables.run_events, tables.runs
    reported = e.c.body["step_report"]["commit_sha"].astext
    found = (
        await conn.execute(
            select(reported)
            .where(e.c.run_id == run_id, e.c.kind == "system", reported.is_not(None))
            .order_by(e.c.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return found or (await conn.execute(select(r.c.commit_sha).where(r.c.id == run_id))).scalar_one_or_none()


async def lines(conn: AsyncConnection, proposal_id: int):
    """The lines of proposal ``proposal_id``'s ledger, oldest first, with the login of their member."""
    lg, u = tables.curator_ledger, tables.users
    query = (
        select(
            *(column for column in lg.c if column.name != "project_id"),
            u.c.login.label("actor_login"),
        )
        .select_from(lg.outerjoin(u, u.c.id == lg.c.actor_id))
        .where(lg.c.proposal_id == proposal_id)
        .order_by(lg.c.id)
    )
    return (await conn.execute(query)).all()


async def merged_change(conn: AsyncConnection, proposal_id: int):
    """(id, merged_at) of the merged change of proposal ``proposal_id``, or None."""
    c = tables.curator_changes
    return (
        await conn.execute(
            select(c.c.id, c.c.merged_at).where(c.c.proposal_id == proposal_id, c.c.merged_at.is_not(None))
        )
    ).one_or_none()


async def outcome_in(conn: AsyncConnection, change_id: int) -> bool:
    """Whether the ledger has the outcome of change ``change_id``."""
    lg = tables.curator_ledger
    return (
        await conn.execute(select(exists().where(lg.c.change_id == change_id, lg.c.action == "outcome")))
    ).scalar_one()
