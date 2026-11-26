"""The hub's rows srs reads about people and runs: members by login, the grants of a project, the notices a member
was sent, and the kind and state of a run."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def user_id(conn: AsyncConnection, login: str) -> int | None:
    """The id of the member whose login is ``login``, exactly; or None."""
    u = tables.users
    return (await conn.execute(select(u.c.id).where(u.c.login == login))).scalar_one_or_none()


async def named(conn: AsyncConnection, logins: Iterable[str]) -> set[int]:
    """The ids of the members ``logins`` names (logins are case-insensitive), those with an account."""
    u = tables.users
    rows = await conn.execute(select(u.c.id).where(func.lower(u.c.login).in_([login.lower() for login in logins])))
    return set(rows.scalars())


async def members(conn: AsyncConnection, project_id: int, logins: set[str]) -> set[int]:
    """The ids of the members of project ``project_id`` among ``logins``, lower case."""
    u, g = tables.users, tables.grants
    query = (
        select(u.c.id)
        .join_from(u, g, g.c.user_id == u.c.id)
        .where(g.c.project_id == project_id, func.lower(u.c.login).in_(sorted(logins)))
    )
    return set((await conn.execute(query)).scalars())


async def grants(conn: AsyncConnection, project_id: int) -> list:
    """(user id, role, max level) of each grant on project ``project_id``, by user."""
    g = tables.grants
    query = select(g.c.user_id, g.c.role, g.c.max_level).where(g.c.project_id == project_id).order_by(g.c.user_id)
    return list(await conn.execute(query))


async def roles(conn: AsyncConnection, project_id: int, ids: set[int]) -> dict[int, str]:
    """The role on project ``project_id`` of each of the members ``ids`` who has a grant on it."""
    g = tables.grants
    query = select(g.c.user_id, g.c.role).where(g.c.project_id == project_id, g.c.user_id.in_(ids or [0]))
    return {row.user_id: row.role for row in await conn.execute(query)}


async def logins_and_roles(conn: AsyncConnection, project_id: int, ids: set[int]) -> list:
    """(id, login, role on project ``project_id`` or None) of each member ``ids`` names."""
    u, g = tables.users, tables.grants
    query = (
        select(u.c.id, u.c.login, g.c.role)
        .join_from(u, g, (g.c.user_id == u.c.id) & (g.c.project_id == project_id), isouter=True)
        .where(u.c.id.in_(ids or [0]))
    )
    return list(await conn.execute(query))


async def noticed(conn: AsyncConnection, user_id: int, kind: str, details: dict[str, str]) -> bool:
    """Whether member ``user_id`` was sent a notice of ``kind`` whose details hold each of ``details`` (as text)."""
    n = tables.notifications
    held = [n.c.details[name].astext == value for name, value in details.items()]
    query = select(n.c.id).where(n.c.user_id == user_id, n.c.notice_kind == kind, *held).limit(1)
    return (await conn.execute(query)).first() is not None


async def run(conn: AsyncConnection, run_id: int):
    """The kind, profile and dispatcher of run ``run_id``."""
    r = tables.runs
    return (await conn.execute(select(r.c.kind, r.c.profile_id, r.c.dispatched_by).where(r.c.id == run_id))).one()


async def run_state(conn: AsyncConnection, run_id: int) -> str | None:
    r = tables.runs
    return await conn.scalar(select(r.c.state).where(r.c.id == run_id))


__all__ = [
    "grants",
    "logins_and_roles",
    "members",
    "named",
    "noticed",
    "roles",
    "run",
    "run_state",
    "user_id",
]
