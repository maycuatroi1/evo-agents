"""The queries of who is in a group chat (``chats.service.members``)."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def role_on(conn: AsyncConnection, user_id: int, project_id: int) -> str | None:
    """The role person ``user_id`` holds on project ``project_id``; None without a grant."""
    g = tables.grants
    query = select(g.c.role).where(g.c.user_id == user_id, g.c.project_id == project_id)
    return (await conn.execute(query)).scalar_one_or_none()


async def user_named(conn: AsyncConnection, login: str):
    """The (id, login) row of the person who signed in as ``login``, ignoring case; None for nobody."""
    u = tables.users
    return (await conn.execute(select(u.c.id, u.c.login).where(func.lower(u.c.login) == login.lower()))).one_or_none()


async def members_now(conn: AsyncConnection, chat_id: int) -> tuple[dict[int, str], list[str]]:
    """({person id: login}, [agent handle]) of the chat now."""
    m, u, ap = tables.chat_members, tables.users, tables.agent_profiles
    query = (
        select(m.c.user_id, u.c.login, ap.c.handle)
        .select_from(m.outerjoin(u, u.c.id == m.c.user_id).outerjoin(ap, ap.c.id == m.c.profile_id))
        .where(m.c.chat_id == chat_id, m.c.removed_at.is_(None))
    )
    people, agents = {}, []
    for row in await conn.execute(query):
        if row.user_id is not None:
            people[row.user_id] = row.login
        else:
            agents.append(row.handle)
    return people, agents
