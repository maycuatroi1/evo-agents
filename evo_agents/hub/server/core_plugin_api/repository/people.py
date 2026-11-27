"""The queries of the reads of API 1.3 added while the core plugins of evo-agents moved onto the API: a project, its
join links, members by login whatever its letter case, the first run each member dispatched, and who a run is for."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.core_plugin_api.models.reads import MAX_ROWS


async def project(conn: AsyncConnection, project: str | int, *, by_id: bool):
    """The id and name of ``project``, by id when ``by_id``, else by name; None without it."""
    p = tables.projects
    by = p.c.id == project if by_id else p.c.name == project
    return (await conn.execute(select(p.c.id, p.c.name).where(by))).one_or_none()


async def invites(conn: AsyncConnection, project_id: int, kinds: Sequence[str], since: datetime | None):
    """The invite links of the project with the login of who accepted each, by id."""
    i, u = tables.invites, tables.users
    query = (
        select(i, u.c.login.label("acceptor"))
        .join_from(i, u, u.c.id == i.c.accepted_by, isouter=True)
        .where(i.c.project_id == project_id)
    )
    if kinds:
        query = query.where(i.c.kind.in_(list(kinds)))
    if since is not None:
        query = query.where(or_(i.c.created_at >= since, i.c.accepted_at >= since))
    return (await conn.execute(query.order_by(i.c.id).limit(MAX_ROWS))).all()


async def users_by_login(conn: AsyncConnection, logins: Sequence[str]):
    """The members whose login in lower case is one of ``logins``, by id."""
    u = tables.users
    query = select(u.c.id, u.c.login, u.c.created_at, u.c.last_seen_at).where(func.lower(u.c.login).in_(list(logins)))
    return (await conn.execute(query.order_by(u.c.id).limit(MAX_ROWS))).all()


async def first_runs(
    conn: AsyncConnection, project_id: int, ids: Sequence[int], since: datetime | None, before: datetime | None
):
    """Rows of a member id among ``ids`` and when the first run they dispatched in the project was queued."""
    r = tables.runs.c
    query = select(r.dispatched_by, func.min(r.queued_at)).where(r.project_id == project_id, r.dispatched_by.in_(ids))
    if since is not None:
        query = query.where(r.queued_at >= since)
    if before is not None:
        query = query.where(r.queued_at < before)
    return (await conn.execute(query.group_by(r.dispatched_by))).all()


async def run_actor(conn: AsyncConnection, run_id: int):
    """The id, kind, profile and dispatcher of run ``run_id`` with its project's name and its dispatcher's login."""
    r, p, u = tables.runs, tables.projects, tables.users
    query = (
        select(r.c.id, r.c.kind, r.c.profile_id, r.c.dispatched_by, p.c.name, u.c.login)
        .join_from(r, p, p.c.id == r.c.project_id)
        .join(u, u.c.id == r.c.dispatched_by, isouter=True)
        .where(r.c.id == run_id)
    )
    return (await conn.execute(query)).one_or_none()
