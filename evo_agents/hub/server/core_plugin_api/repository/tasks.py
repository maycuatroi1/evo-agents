"""The queries of the reads of API 1.3 on a project's tasks: the tasks with their sprint, the sprints, the people,
events, latest comment and run, open blockers, decisions and SRS items of each task, the revisions of plans, a chat
and the caller of a tool. Each keeps to the tasks of one project, whatever ids it is given."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables

_T = tables.metadata.tables


def _of_project(query, task_id, project_id: int):
    """``query`` kept to the tasks of project ``project_id``, joined on ``task_id``."""
    t = _T["tasks_items"]
    return query.join(t, t.c.id == task_id).where(t.c.project_id == project_id)


async def task_details(conn: AsyncConnection, project_id: int, limit: int):
    """The tasks of the project with their sprint's name, by number."""
    t, s = _T["tasks_items"], _T["tasks_sprints"]
    query = (
        select(t, s.c.name.label("sprint"))
        .select_from(t.outerjoin(s, s.c.id == t.c.sprint_id))
        .where(t.c.project_id == project_id)
        .order_by(t.c.number)
        .limit(limit)
    )
    return (await conn.execute(query)).all()


async def sprint_details(conn: AsyncConnection, project_id: int, states: Sequence[str]):
    """The sprints of the project, newest first: in one of ``states``."""
    s = _T["tasks_sprints"]
    query = select(s).where(s.c.project_id == project_id)
    if states:
        query = query.where(s.c.state.in_(list(states)))
    return (await conn.execute(query.order_by(s.c.id.desc()))).all()


async def task_people(conn: AsyncConnection, project_id: int, ids: Sequence[int], roles: Sequence[str]):
    """The people of the project's tasks among ``ids`` with their login, by task and login: in one of ``roles``."""
    p, u = _T["tasks_people"], tables.users
    query = _of_project(
        select(p.c.task_id, p.c.role, p.c.user_id, u.c.login).join_from(p, u, u.c.id == p.c.user_id),
        p.c.task_id,
        project_id,
    )
    query = query.where(p.c.task_id.in_(list(ids)))
    if roles:
        query = query.where(p.c.role.in_(list(roles)))
    return (await conn.execute(query.order_by(p.c.task_id, u.c.login))).all()


async def task_events(conn: AsyncConnection, project_id: int, ids: Sequence[int]):
    """The events of the project's tasks among ``ids``, oldest first."""
    e = _T["tasks_events"]
    query = _of_project(
        select(e.c.id, e.c.task_id, e.c.field, e.c.from_value, e.c.to_value, e.c.at).select_from(e),
        e.c.task_id,
        project_id,
    )
    return (await conn.execute(query.where(e.c.task_id.in_(list(ids))).order_by(e.c.id))).all()


async def last_comments(conn: AsyncConnection, project_id: int, ids: Sequence[int]) -> dict[int, datetime]:
    """When the latest comment on each of the project's tasks among ``ids`` was made, by task."""
    c = _T["tasks_comments"]
    query = _of_project(
        select(c.c.task_id, func.max(c.c.created_at).label("at")).select_from(c), c.c.task_id, project_id
    )
    found = await conn.execute(query.where(c.c.task_id.in_(list(ids))).group_by(c.c.task_id))
    return {row.task_id: row.at for row in found}


async def last_runs(conn: AsyncConnection, project_id: int, ids: Sequence[int]) -> dict[int, datetime]:
    """The latest move of a run of each of the project's tasks among ``ids`` (one it links, or one of a plan step it
    links), by task."""
    k, r = _T["tasks_links"], tables.runs
    moved = func.greatest(r.c.queued_at, r.c.leased_at, r.c.started_at, r.c.finished_at)
    by_run = and_(k.c.kind == "run", k.c.run_id == r.c.id)
    by_step = and_(k.c.kind == "plan_step", k.c.plan_id == r.c.plan_id, k.c.step_id == r.c.step_key)
    query = _of_project(
        select(k.c.task_id, func.max(moved).label("at")).select_from(k.join(r, or_(by_run, by_step))),
        k.c.task_id,
        project_id,
    )
    found = await conn.execute(query.where(k.c.task_id.in_(list(ids))).group_by(k.c.task_id))
    return {row.task_id: row.at for row in found}


async def task_blockers(conn: AsyncConnection, project_id: int, ids: Sequence[int]):
    """The open blockers of the project's tasks among ``ids`` with who set them and what they wait on, oldest
    first."""
    b, u, t = _T["tasks_blockers"], tables.users, _T["tasks_items"]
    setter, waiter, other = u.alias("setter"), u.alias("waiter"), t.alias("other")
    query = (
        select(
            b.c.id,
            b.c.task_id,
            b.c.reason,
            b.c.since,
            b.c.waiting_text,
            setter.c.login.label("set_by"),
            waiter.c.login.label("waiting_login"),
            other.c.number.label("waiting_number"),
        )
        .select_from(
            b.join(t, t.c.id == b.c.task_id)
            .join(setter, setter.c.id == b.c.created_by)
            .outerjoin(waiter, waiter.c.id == b.c.waiting_user_id)
            .outerjoin(other, other.c.id == b.c.waiting_task_id)
        )
        .where(t.c.project_id == project_id, b.c.task_id.in_(list(ids)), b.c.cleared_at.is_(None))
        .order_by(b.c.since, b.c.id)
    )
    return (await conn.execute(query)).all()


async def task_decisions(conn: AsyncConnection, project_id: int, ids: Sequence[int], states: Sequence[str]):
    """The decisions of the runs of the project's tasks among ``ids`` with their dispatcher's login, oldest first: in
    one of ``states`` (all with none)."""
    d, r, k, u, t = tables.decisions, tables.runs, _T["tasks_links"], tables.users, _T["tasks_items"]
    by_run = and_(k.c.kind == "run", k.c.run_id == r.c.id)
    by_step = and_(k.c.kind == "plan_step", k.c.plan_id == r.c.plan_id, k.c.step_id == r.c.step_key)
    query = (
        select(k.c.task_id, d.c.id.label("decision_id"), d.c.run_id, d.c.question, d.c.state, d.c.asked_at, u.c.login)
        .select_from(
            d.join(r, r.c.id == d.c.run_id)
            .join(k, or_(by_run, by_step))
            .join(t, t.c.id == k.c.task_id)
            .join(u, u.c.id == r.c.dispatched_by)
        )
        .where(t.c.project_id == project_id, k.c.task_id.in_(list(ids)))
        .distinct()
        .order_by(d.c.asked_at, d.c.id)
    )
    if states:
        query = query.where(d.c.state.in_(list(states)))
    return (await conn.execute(query)).all()


async def task_srs_items(conn: AsyncConnection, project_id: int, ids: Sequence[int]):
    """The SRS items the project's tasks among ``ids`` link, with their title at the newest version of their document
    and its slug and label, by task and link."""
    k, i, doc, iv, v = (
        _T["tasks_links"],
        _T["srs_items"],
        _T["srs_documents"],
        _T["srs_item_versions"],
        _T["srs_versions"],
    )
    title = (
        select(iv.c.title)
        .join(v, v.c.id == iv.c.version_id)
        .where(iv.c.item_id == i.c.id)
        .order_by(v.c.number.desc())
        .limit(1)
        .scalar_subquery()
    )
    query = (
        select(k.c.task_id, i.c.id, doc.c.slug, i.c.kind, i.c.code, i.c.retired_in, doc.c.label, title.label("title"))
        .select_from(k.join(i, i.c.id == k.c.srs_item_id).join(doc, doc.c.id == i.c.document_id))
        .where(i.c.project_id == project_id, k.c.task_id.in_(list(ids)), k.c.kind == "srs_item")
        .order_by(k.c.task_id, k.c.id)
    )
    return (await conn.execute(query)).all()


_REVISION = (tables.plan_revisions.c.plan_id, tables.plan_revisions.c.revision, tables.plan_revisions.c.created_at)


async def plan_revisions(conn: AsyncConnection, project_id: int, plans: Sequence[str], since: datetime):
    """The revisions of the project's plans among ``plans`` made at or after ``since``, by plan and revision."""
    rv = tables.plan_revisions
    query = select(*_REVISION, rv.c.body).where(
        rv.c.project_id == project_id, rv.c.plan_id.in_(list(plans)), rv.c.created_at >= since
    )
    return (await conn.execute(query.order_by(rv.c.plan_id, rv.c.revision))).all()


async def plan_revision(conn: AsyncConnection, project_id: int, plan_id: str, revision: int):
    """Revision ``revision`` of the project's plan ``plan_id``, as rows (none or one)."""
    rv = tables.plan_revisions
    query = select(*_REVISION, rv.c.body).where(
        rv.c.project_id == project_id, rv.c.plan_id == plan_id, rv.c.revision == revision
    )
    return (await conn.execute(query)).all()


async def chat_exists(conn: AsyncConnection, chat_id: int) -> bool:
    """Whether chat ``chat_id`` exists."""
    c = tables.chats
    return await conn.scalar(select(c.c.id).where(c.c.id == chat_id)) is not None


async def user_id_of(conn: AsyncConnection, login: str) -> int | None:
    """The id of the member of login ``login``, None without one."""
    u = tables.users
    return (await conn.execute(select(u.c.id).where(u.c.login == login))).scalar_one_or_none()


async def run_kind(conn: AsyncConnection, run_id: int) -> str | None:
    """The kind of run ``run_id``, None without it."""
    r = tables.runs
    return (await conn.execute(select(r.c.kind).where(r.c.id == run_id))).scalar_one_or_none()
