"""What a judge run of a change and the routes of the Curator's changes (``evo_agents.hub.server.changes.service.judge``
and ``service.manage``) read: the run, the plan revision it judges, the proposal it reads, and a project's changes."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.changes.repository.views import changes_query


async def locked_run(conn: AsyncConnection, run_id: int):
    """(id, state, worker_id, kind, project_id, plan_id, plan_revision) of run ``run_id``, locked; None without it."""
    r = tables.runs
    return (
        await conn.execute(
            select(r.c.id, r.c.state, r.c.worker_id, r.c.kind, r.c.project_id, r.c.plan_id, r.c.plan_revision)
            .where(r.c.id == run_id)
            .with_for_update()
        )
    ).one_or_none()


async def run_row(conn: AsyncConnection, run_id: int):
    """(worker_id, state, kind, project_id, plan_id) of run ``run_id``; None without it."""
    r = tables.runs
    query = select(r.c.worker_id, r.c.state, r.c.kind, r.c.project_id, r.c.plan_id).where(r.c.id == run_id)
    return (await conn.execute(query)).one_or_none()


async def revision_body(conn: AsyncConnection, project_id: int, plan_id: str, revision: int):
    """The body of revision ``revision`` of plan ``plan_id``; None without one."""
    rev = tables.plan_revisions
    return (
        await conn.execute(
            select(rev.c.body).where(
                rev.c.project_id == project_id, rev.c.plan_id == plan_id, rev.c.revision == revision
            )
        )
    ).scalar_one_or_none()


async def judge_proposal(conn: AsyncConnection, proposal_id: int):
    """(id, title, kind, tier, summary, paths) of proposal ``proposal_id``, as a judge run reads it."""
    pr = tables.proposals
    return (
        await conn.execute(
            select(pr.c.id, pr.c.title, pr.c.kind, pr.c.tier, pr.c.summary, pr.c.paths).where(pr.c.id == proposal_id)
        )
    ).one()


async def project_changes(conn: AsyncConnection, project_id: int, limit: int) -> list:
    """The project's changes, newest first, at most ``limit``, each with its proposal's label."""
    c, pr = tables.curator_changes, tables.proposals
    query = (
        changes_query()
        .add_columns(pr.c.label.label("proposal_label"))
        .join(pr, pr.c.id == c.c.proposal_id)
        .where(c.c.project_id == project_id)
        .order_by(c.c.id.desc())
        .limit(limit)
    )
    return list((await conn.execute(query)).all())


async def locked_change(conn: AsyncConnection, change_id: int, project_id: int):
    """The row of change ``change_id`` of the project with its proposal's label, locked; None without it."""
    c, pr = tables.curator_changes, tables.proposals
    query = (
        select(c, pr.c.label.label("proposal_label"))
        .join_from(c, pr, pr.c.id == c.c.proposal_id)
        .where(c.c.id == change_id, c.c.project_id == project_id)
        .with_for_update(of=c)
    )
    return (await conn.execute(query)).one_or_none()
