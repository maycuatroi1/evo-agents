"""The queries of the hub's data a core plugin reads (API 1.3): members, runs, grants, plans, decisions, signals,
goals, canvas claims, tasks and SRS, each of one project and at most ``MAX_ROWS`` rows."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.core_plugin_api.models.reads import MAX_ROWS

_T = tables.metadata.tables


async def users(conn: AsyncConnection, ids: Sequence[int], logins: Sequence[str]):
    """The members with id in ``ids`` or login in ``logins``, by id."""
    u = tables.users
    query = select(u.c.id, u.c.login, u.c.created_at, u.c.last_seen_at).where(u.c.id.in_(ids) | u.c.login.in_(logins))
    return (await conn.execute(query.order_by(u.c.id).limit(MAX_ROWS))).all()


async def runs(conn: AsyncConnection, project_id: int, since: datetime | None, kinds: Sequence[str], limit: int):
    """The runs of the project with their dispatcher's login, newest first."""
    r, u = tables.runs, tables.users
    query = (
        select(r, u.c.login.label("dispatcher"))
        .join_from(r, u, u.c.id == r.c.dispatched_by, isouter=True)
        .where(r.c.project_id == project_id)
    )
    if since is not None:
        query = query.where(r.c.queued_at >= since)
    if kinds:
        query = query.where(r.c.kind.in_(list(kinds)))
    return (await conn.execute(query.order_by(r.c.id.desc()).limit(limit))).all()


async def grants(conn: AsyncConnection, project_id: int):
    """The grants on the project with their member's login, by login."""
    g, u = tables.grants, tables.users
    query = (
        select(u.c.login, g.c.user_id, g.c.role, g.c.max_level, g.c.granted_at)
        .join_from(g, u, u.c.id == g.c.user_id)
        .where(g.c.project_id == project_id)
        .order_by(u.c.login)
    )
    return (await conn.execute(query)).all()


async def plans(conn: AsyncConnection, project_id: int):
    """The plans of the project, last updated first."""
    p = tables.plans
    query = select(p).where(p.c.project_id == project_id).order_by(p.c.updated_at.desc()).limit(MAX_ROWS)
    return (await conn.execute(query)).all()


async def plan_body(conn: AsyncConnection, project_id: int, plan: str):
    """The body of plan ``plan`` of the project, None without it."""
    p = tables.plans
    query = select(p.c.body).where(p.c.project_id == project_id, p.c.plan_id == plan)
    return (await conn.execute(query)).scalar_one_or_none()


async def decisions(conn: AsyncConnection, project_id: int, since: datetime | None, states: Sequence[str], limit: int):
    """The decisions of the project's runs, newest first."""
    d = tables.decisions
    query = select(d).where(d.c.project_id == project_id)
    if since is not None:
        query = query.where(d.c.asked_at >= since)
    if states:
        query = query.where(d.c.state.in_(list(states)))
    return (await conn.execute(query.order_by(d.c.id.desc()).limit(limit))).all()


async def signal_values(conn: AsyncConnection, project_id: int, key: str, since: datetime | None, limit: int):
    """The values of the project's signal ``key``, last measured first."""
    s, k = tables.signals, tables.signal_keys
    query = (
        select(s, k.c.name.label("key"))
        .join_from(s, k, k.c.id == s.c.key_id)
        .where(s.c.project_id == project_id, k.c.name == key)
    )
    if since is not None:
        query = query.where(s.c.measured_at >= since)
    return (await conn.execute(query.order_by(s.c.measured_at.desc(), s.c.id.desc()).limit(limit))).all()


async def goals(conn: AsyncConnection, project_id: int):
    """The goals of the project at their current revision with their owner's login, by number."""
    g, rev, u = tables.goals, tables.goal_revisions, tables.users
    query = (
        select(g.c.id, g.c.number, g.c.alias, g.c.revision, rev, u.c.login.label("owner"))
        .join_from(g, rev, (rev.c.goal_id == g.c.id) & (rev.c.revision == g.c.revision))
        .join(u, u.c.id == rev.c.owner_id, isouter=True)
        .where(g.c.project_id == project_id)
        .order_by(g.c.number)
    )
    return (await conn.execute(query)).all()


async def claims(conn: AsyncConnection, project_id: int, canvas: int | None, statuses: Sequence[str]):
    """The claims of the project's canvases that are not archived, by canvas, cell and position."""
    c = _T["canvas_claims"]
    query = select(c).where(c.c.project_id == project_id, c.c.archived_at.is_(None))
    if canvas is not None:
        query = query.where(c.c.canvas_id == canvas)
    if statuses:
        query = query.where(c.c.status.in_(list(statuses)))
    return (await conn.execute(query.order_by(c.c.canvas_id, c.c.cell, c.c.position).limit(MAX_ROWS))).all()


async def tasks(conn: AsyncConnection, project_id: int, sprint: int | None, statuses: Sequence[str], limit: int):
    """The tasks of the project, last updated first."""
    t = _T["tasks_items"]
    query = select(t).where(t.c.project_id == project_id)
    if sprint is not None:
        query = query.where(t.c.sprint_id == sprint)
    if statuses:
        query = query.where(t.c.status.in_(list(statuses)))
    return (await conn.execute(query.order_by(t.c.updated_at.desc(), t.c.id.desc()).limit(limit))).all()


async def sprints(conn: AsyncConnection, project_id: int):
    """The sprints of the project, newest first."""
    s = _T["tasks_sprints"]
    return (await conn.execute(select(s).where(s.c.project_id == project_id).order_by(s.c.id.desc()))).all()


async def task_links(conn: AsyncConnection, project_id: int, ids: Sequence[int]):
    """The links of the project's tasks among ``ids``, by task."""
    t, link = _T["tasks_items"], _T["tasks_links"]
    query = (
        select(link)
        .join_from(link, t, t.c.id == link.c.task_id)
        .where(t.c.project_id == project_id, link.c.task_id.in_(list(ids)))
        .order_by(link.c.task_id, link.c.id)
        .limit(MAX_ROWS)
    )
    return (await conn.execute(query)).all()


async def document_id(conn: AsyncConnection, project_id: int, slug: str) -> int | None:
    """The id of the project's SRS document ``slug``, None without it."""
    d = _T["srs_documents"]
    query = select(d.c.id).where(d.c.project_id == project_id, d.c.slug == slug)
    return (await conn.execute(query)).scalar_one_or_none()


async def srs_items(conn: AsyncConnection, project_id: int, document: int | None):
    """The items of the project's SRS documents with their document's slug, of document id ``document`` only, by
    document and key."""
    i, d = _T["srs_items"], _T["srs_documents"]
    query = select(i, d.c.slug).join_from(i, d, d.c.id == i.c.document_id).where(i.c.project_id == project_id)
    if document is not None:
        query = query.where(i.c.document_id == document)
    return (await conn.execute(query.order_by(d.c.slug, i.c.key).limit(MAX_ROWS))).all()


async def srs_versions(conn: AsyncConnection, document: int):
    """The versions of SRS document id ``document``, newest first."""
    v = _T["srs_versions"]
    return (await conn.execute(select(v).where(v.c.document_id == document).order_by(v.c.number.desc()))).all()
