"""The queries of what the weekly probe report asks of a review run: the rows of the weeks it reads, whether a change's
fix was answered, its open fix proposal and the night of a review run."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.probes.models.fixes import OPEN


async def week_rows(conn: AsyncConnection, project_id: int, weeks: list[date]) -> list:
    """The rows of ``weeks`` of the project's active probes of merged changes, with each probe's label, repo and task
    and the title of its change's proposal, by change, probe and row."""
    pw, pr, c, p = tables.curator_probe_runs, tables.curator_probes, tables.curator_changes, tables.proposals
    query = (
        select(
            pw.c.probe_id,
            pw.c.change_id,
            pw.c.week,
            pw.c.runtime,
            pw.c.model,
            pw.c.state,
            pw.c.sessions,
            pw.c.k,
            pw.c.run_id,
            pw.c.reason,
            pr.c.label,
            pr.c.repo,
            pr.c.task,
            p.c.title,
        )
        .join_from(pw, pr, pr.c.id == pw.c.probe_id)
        .join(c, c.c.id == pw.c.change_id)
        .join(p, p.c.id == c.c.proposal_id)
        .where(
            pw.c.project_id == project_id,
            pw.c.week.in_(weeks),
            pr.c.state == "active",
            pr.c.change_id == pw.c.change_id,
            c.c.state == "merged",
        )
        .order_by(pw.c.change_id, pw.c.probe_id, pw.c.id)
    )
    return list((await conn.execute(query)).all())


async def answered(conn: AsyncConnection, change_id: int, since: date) -> bool:
    """Whether the change has an open fix proposal, or a review run of a night since ``since`` recorded a fix proposal
    or a finding for it."""
    p, f, r = tables.proposals, tables.findings, tables.runs
    open_fix = select(p.c.id).where(p.c.fix_of == change_id, p.c.state.in_(OPEN))
    if (await conn.execute(open_fix.limit(1))).first() is not None:
        return True
    for t in (p, f):
        query = (
            select(t.c.id)
            .join_from(t, r, r.c.id == t.c.run_id)
            .where(t.c.fix_of == change_id, func.coalesce(r.c.schedule_night, func.current_date()) >= since)
            .limit(1)
        )
        if (await conn.execute(query)).first() is not None:
            return True
    return False


async def open_fix(conn: AsyncConnection, change_id: int) -> int | None:
    """The open (or deferred) fix proposal of change ``change_id``, if any."""
    p = tables.proposals
    query = select(p.c.id).where(p.c.fix_of == change_id, p.c.state.in_(OPEN)).order_by(p.c.id).limit(1)
    return (await conn.execute(query)).scalar_one_or_none()


async def run_night(conn: AsyncConnection, run_id: int) -> date:
    """The night of review run ``run_id``: its schedule's night, else today in the database."""
    r = tables.runs
    night = (await conn.execute(select(r.c.schedule_night).where(r.c.id == run_id))).scalar_one_or_none()
    return night if night is not None else await conn.scalar(select(func.current_date()))  # pyright: ignore[reportReturnType]
