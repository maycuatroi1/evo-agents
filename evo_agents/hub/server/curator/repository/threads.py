"""The SQL of the threads of the Curator's proposals: who answers a project's proposals, the thread kept for each
proposal, the facts of a proposal and of a change the hub left open, and the grant of the owner the thread answers
for."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def newest_charter_body(conn: AsyncConnection, project_id: int) -> dict | None:
    """The body of the project's newest charter; None without one."""
    c = tables.charters
    query = select(c.c.body).where(c.c.project_id == project_id).order_by(c.c.revision.desc()).limit(1)
    return (await conn.execute(query)).scalar_one_or_none()


async def thread_row(conn: AsyncConnection, proposal_id: int):
    """The chat and the root message of the thread kept for the proposal; None without one."""
    t = tables.proposal_threads
    query = select(t.c.chat_id, t.c.root_message_id).where(t.c.proposal_id == proposal_id)
    return (await conn.execute(query)).one_or_none()


async def proposal_row(conn: AsyncConnection, proposal_id: int):
    """The facts of the proposal a thread's message names, with its project and the owner of its review run."""
    p, r, pr, u = tables.proposals, tables.runs, tables.projects, tables.users
    query = (
        select(
            p.c.id,
            p.c.project_id,
            pr.c.name.label("project"),
            p.c.state,
            p.c.title,
            p.c.tier,
            p.c.kind,
            p.c.lens,
            p.c.tier_reasons,
            p.c.summary,
            p.c.paths,
            p.c.evidence,
            p.c.evidence_count,
            p.c.finding_ids,
            p.c.draft,
            p.c.promotion,
            p.c.system_change,
            r.c.dispatched_by.label("owner_id"),
            u.c.login.label("owner"),
        )
        .join_from(p, pr, pr.c.id == p.c.project_id)
        .join(r, r.c.id == p.c.run_id)
        .join(u, u.c.id == r.c.dispatched_by)
        .where(p.c.id == proposal_id)
    )
    return (await conn.execute(query)).one()


async def role(conn: AsyncConnection, user_id: int, project_id: int) -> str | None:
    g = tables.grants
    query = select(g.c.role).where(g.c.user_id == user_id, g.c.project_id == project_id)
    return (await conn.execute(query)).scalar_one_or_none()


async def keep_thread(conn: AsyncConnection, proposal_id: int, chat_id: int, root_id: int, agent_run: int | None):
    """Keep the thread as the proposal's, with the chat run its latest mention made."""
    t = tables.proposal_threads
    values = {"chat_id": chat_id, "root_message_id": root_id, "agent_run_id": agent_run}
    statement = pg_insert(t).values(proposal_id=proposal_id, **values)
    await conn.execute(
        statement.on_conflict_do_update(index_elements=[t.c.proposal_id], set_={**values, "asked_at": func.now()})
    )


async def change_row(conn: AsyncConnection, change_id: int):
    """The change of the Curator, with the review run of its proposal (``review_run``)."""
    c, p = tables.curator_changes, tables.proposals
    query = select(c, p.c.run_id.label("review_run")).join_from(c, p, p.c.id == c.c.proposal_id)
    return (await conn.execute(query.where(c.c.id == change_id))).one()


async def thread_proposal(conn: AsyncConnection, chat_id: int, root_id: int) -> int | None:
    """The proposal whose thread is the one of ``root_id`` in chat ``chat_id``; None when it is no proposal's."""
    t = tables.proposal_threads
    query = select(t.c.proposal_id).where(t.c.chat_id == chat_id, t.c.root_message_id == root_id)
    return (await conn.execute(query)).scalar_one_or_none()


async def proposal_project(conn: AsyncConnection, proposal_id: int):
    """The id and name of the proposal's project."""
    p, pr = tables.proposals, tables.projects
    query = select(p.c.project_id, pr.c.name).join_from(p, pr, pr.c.id == p.c.project_id).where(p.c.id == proposal_id)
    return (await conn.execute(query)).one()
