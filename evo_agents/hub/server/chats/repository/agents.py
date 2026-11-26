"""The queries of the agent of a run in group chats (``chats.service.agents``, ``chats.service.pulls``)."""

from __future__ import annotations

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def held_row(conn: AsyncConnection, run_id: int):
    """Run ``run_id`` as ``agent_run`` reads it, with its dispatcher's login, its row locked; None for none."""
    r, u = tables.runs, tables.users
    query = (
        select(
            r.c.state,
            r.c.worker_id,
            r.c.kind,
            r.c.project_id,
            r.c.profile_id,
            r.c.dispatched_by,
            u.c.login,
            r.c.dispatched_via,
            r.c.hop,
            r.c.branch,
            r.c.repos,
        )
        .join_from(r, u, u.c.id == r.c.dispatched_by)
        .where(r.c.id == run_id)
        .with_for_update(of=r)
    )
    return (await conn.execute(query)).one_or_none()


async def is_member(conn: AsyncConnection, chat_id: int, user_id: int) -> bool:
    """Whether person ``user_id`` is in chat ``chat_id`` now."""
    m = tables.chat_members
    member = select(m.c.id).where(m.c.chat_id == chat_id, m.c.user_id == user_id, m.c.removed_at.is_(None))
    return (await conn.execute(member)).first() is not None


async def chat_of_message(conn: AsyncConnection, message_id: int) -> int | None:
    """The chat of message ``message_id``; None for no such message."""
    cm = tables.chat_messages
    return (await conn.execute(select(cm.c.chat_id).where(cm.c.id == message_id))).scalar_one_or_none()


async def insert_chat(conn: AsyncConnection, title: str, created_by: int) -> int:
    """A new chat titled ``title`` that ``created_by`` created; its id."""
    c = tables.chats
    return (await conn.execute(insert(c).values(title=title, created_by=created_by).returning(c.c.id))).scalar_one()


async def repo_row(conn: AsyncConnection, project_id: int, name: str):
    """The origin and default branch of repo ``name`` of project ``project_id``; None for a repo it does not list."""
    pr = tables.project_repos
    query = select(pr.c.origin, pr.c.default_branch).where(pr.c.project_id == project_id, pr.c.name == name)
    return (await conn.execute(query)).one_or_none()


async def run_title(conn: AsyncConnection, run_id: int) -> str | None:
    r = tables.runs
    return (await conn.execute(select(r.c.title).where(r.c.id == run_id))).scalar_one_or_none()


async def set_repos(conn: AsyncConnection, run_id: int, repos: list[dict]) -> None:
    r = tables.runs
    await conn.execute(update(r).values(repos=repos).where(r.c.id == run_id))
