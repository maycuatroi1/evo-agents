"""The hub's data as a core plugin reads it (API 1.3): members, runs, grants, plans and their steps, decisions,
signals, goals, canvas claims, tasks and SRS. Each read names a project that enables the plugin (404 otherwise, with
the words of ``plugin_config``) and returns frozen values, newest first where an order matters. The rows carry their
sensitivity label: what a reader may see of them is the plugin's to check, with ``project_access`` and ``restricted``,
as the hub's routes do. Private to ``core_plugin_api``."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from types import MappingProxyType

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import goals as hub_goals
from evo_agents.hub import plans as hub_plans
from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server.core_plugin_api._types import (
    ClaimRef,
    DecisionRef,
    GoalRef,
    GrantRef,
    PlanRef,
    Plugin,
    Refused,
    RunRef,
    SignalValue,
    SprintRef,
    SrsItemRef,
    SrsVersionRef,
    StepRef,
    TaskLinkRef,
    TaskRef,
    UserRef,
    own_plugin,
)
from evo_agents.hub.server.plugin_routes import not_enabled

MAX_ROWS = 5000  # the most rows one read returns
NO_PLAN = "project {project} has no plan {plan}"
NO_DOCUMENT = "project {project} has no SRS document {document}"

_T = tables.metadata.tables


def _frozen(value) -> MappingProxyType:
    return MappingProxyType(dict(value or {}))


def _limit(limit: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_ROWS:
        raise ValueError(f"a read's limit is from 1 to {MAX_ROWS}")
    return limit


async def enabled_project(conn: AsyncConnection, plugin: Plugin, project: str) -> int:
    """The id of ``project``, which must enable the plugin: 404 otherwise."""
    own_plugin(plugin)
    p = tables.projects
    project_id = (await conn.execute(select(p.c.id).where(p.c.name == project))).scalar_one_or_none()
    if project_id is None or await enabled_in(conn, plugin.id, project_id) is None:
        failed = not_enabled(plugin.id, project)
        raise Refused(failed.status_code, failed.detail)
    return project_id


# Members, runs, grants


async def read_users(conn: AsyncConnection, plugin: Plugin, *, ids: Iterable[int] = (), logins: Iterable[str] = ()):
    """The members of the hub with id in ``ids`` or login in ``logins``, by id; those that do not exist are left
    out."""
    own_plugin(plugin)
    ids, logins = list(ids), list(logins)
    if not ids and not logins:
        return ()
    u = tables.users
    query = select(u.c.id, u.c.login, u.c.created_at, u.c.last_seen_at).where(u.c.id.in_(ids) | u.c.login.in_(logins))
    rows = (await conn.execute(query.order_by(u.c.id).limit(MAX_ROWS))).all()
    return tuple(UserRef(r.id, r.login, r.created_at, r.last_seen_at) for r in rows)


async def read_runs(
    conn: AsyncConnection,
    plugin: Plugin,
    project: str,
    *,
    since: datetime | None = None,
    kinds: Sequence[str] = (),
    limit: int = 500,
) -> tuple[RunRef, ...]:
    """The runs of ``project``, newest first: those queued at or after ``since``, of one of ``kinds``."""
    project_id = await enabled_project(conn, plugin, project)
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
    rows = (await conn.execute(query.order_by(r.c.id.desc()).limit(_limit(limit)))).all()
    return tuple(
        RunRef(
            row.id,
            project,
            row.kind,
            row.state,
            row.title,
            row.plan_id,
            row.step_key,
            row.dispatcher,
            row.queued_at,
            row.finished_at,
        )
        for row in rows
    )


async def read_grants(conn: AsyncConnection, plugin: Plugin, project: str) -> tuple[GrantRef, ...]:
    """The members holding a grant on ``project``, by login."""
    project_id = await enabled_project(conn, plugin, project)
    g, u = tables.grants, tables.users
    query = (
        select(u.c.login, g.c.user_id, g.c.role, g.c.max_level, g.c.granted_at)
        .join_from(g, u, u.c.id == g.c.user_id)
        .where(g.c.project_id == project_id)
        .order_by(u.c.login)
    )
    rows = (await conn.execute(query)).all()
    return tuple(GrantRef(r.login, r.user_id, r.role, r.max_level, r.granted_at) for r in rows)


# Plans and decisions


def _plan(row) -> PlanRef:
    body = row.body if isinstance(row.body, dict) else {}
    return PlanRef(
        row.plan_id,
        row.area,
        row.revision,
        body.get("status"),
        body.get("title"),
        _frozen(row.label),
        row.updated_at,
    )


async def read_plans(
    conn: AsyncConnection, plugin: Plugin, project: str, *, statuses: Sequence[str] = ()
) -> tuple[PlanRef, ...]:
    """The plans of ``project``, last updated first; with ``statuses``, those whose body's status is one of them."""
    project_id = await enabled_project(conn, plugin, project)
    p = tables.plans
    query = select(p).where(p.c.project_id == project_id).order_by(p.c.updated_at.desc()).limit(MAX_ROWS)
    found = tuple(_plan(row) for row in (await conn.execute(query)).all())
    return tuple(plan for plan in found if plan.status in statuses) if statuses else found


async def read_plan_steps(conn: AsyncConnection, plugin: Plugin, project: str, plan: str) -> tuple[StepRef, ...]:
    """The steps of plan ``plan`` of ``project``, in its order; 404 when it has no such plan."""
    project_id = await enabled_project(conn, plugin, project)
    p = tables.plans
    query = select(p.c.body).where(p.c.project_id == project_id, p.c.plan_id == plan)
    body = (await conn.execute(query)).scalar_one_or_none()
    if body is None:
        raise Refused(404, NO_PLAN.format(project=project, plan=plan))
    steps = body.get("steps") if isinstance(body, dict) else None
    found = []
    for index, step in enumerate(steps if isinstance(steps, list) else []):
        data = step if isinstance(step, dict) else {}
        depends = data.get("depends_on") or ()
        found.append(
            StepRef(
                plan,
                hub_plans.step_key(step, index),
                index,
                data.get("title"),
                data.get("status"),
                tuple(str(item) for item in (depends if isinstance(depends, list) else [depends])),
                _frozen(data),
            )
        )
    return tuple(found)


def step_key(step, index: int) -> str:
    """How plans name a step: its id, else its order, else its position."""
    return hub_plans.step_key(step, index)


async def read_decisions(
    conn: AsyncConnection,
    plugin: Plugin,
    project: str,
    *,
    since: datetime | None = None,
    states: Sequence[str] = (),
    limit: int = 500,
) -> tuple[DecisionRef, ...]:
    """The decisions of ``project``'s runs, newest first: asked at or after ``since``, in one of ``states``."""
    project_id = await enabled_project(conn, plugin, project)
    d = tables.decisions
    query = select(d).where(d.c.project_id == project_id)
    if since is not None:
        query = query.where(d.c.asked_at >= since)
    if states:
        query = query.where(d.c.state.in_(list(states)))
    rows = (await conn.execute(query.order_by(d.c.id.desc()).limit(_limit(limit)))).all()
    return tuple(
        DecisionRef(
            r.id,
            r.run_id,
            r.plan_id,
            r.step_key,
            r.category,
            r.question,
            r.state,
            r.answer_option,
            r.asked_at,
            r.answered_at,
        )
        for r in rows
    )


# Signals, goals, canvas claims


async def read_signals(
    conn: AsyncConnection,
    plugin: Plugin,
    project: str,
    key: str,
    *,
    since: datetime | None = None,
    limit: int = 500,
) -> tuple[SignalValue, ...]:
    """The values of ``project``'s signal ``key``, last measured first: those measured at or after ``since``. None
    when the project has no such key."""
    project_id = await enabled_project(conn, plugin, project)
    s, k = tables.signals, tables.signal_keys
    query = (
        select(s, k.c.name.label("key"))
        .join_from(s, k, k.c.id == s.c.key_id)
        .where(s.c.project_id == project_id, k.c.name == key)
    )
    if since is not None:
        query = query.where(s.c.measured_at >= since)
    rows = (await conn.execute(query.order_by(s.c.measured_at.desc(), s.c.id.desc()).limit(_limit(limit)))).all()
    return tuple(
        SignalValue(
            r.id,
            r.key,
            None if r.value is None else float(r.value),
            r.text,
            r.source,
            r.tenant,
            r.measured_at,
            _frozen(r.label),
        )
        for r in rows
    )


async def read_goals(conn: AsyncConnection, plugin: Plugin, project: str) -> tuple[GoalRef, ...]:
    """The goals of ``project`` at their current revision, by number."""
    project_id = await enabled_project(conn, plugin, project)
    g, rev, u = tables.goals, tables.goal_revisions, tables.users
    query = (
        select(g.c.id, g.c.number, g.c.alias, g.c.revision, rev, u.c.login.label("owner"))
        .join_from(g, rev, (rev.c.goal_id == g.c.id) & (rev.c.revision == g.c.revision))
        .join(u, u.c.id == rev.c.owner_id, isouter=True)
        .where(g.c.project_id == project_id)
        .order_by(g.c.number)
    )
    rows = (await conn.execute(query)).all()
    return tuple(
        GoalRef(
            r.id,
            hub_goals.code(r.number),
            r.alias,
            r.revision,
            r.statement,
            None if r.metric is None else _frozen(r.metric),
            hub_goals.metric_text(r.metric),
            r.deadline,
            r.status,
            r.owner,
        )
        for r in rows
    )


async def read_claims(
    conn: AsyncConnection,
    plugin: Plugin,
    project: str,
    *,
    canvas: int | None = None,
    statuses: Sequence[str] = (),
) -> tuple[ClaimRef, ...]:
    """The claims of ``project``'s canvases that are not archived, by canvas, cell and position: of canvas id
    ``canvas`` only, in one of ``statuses``."""
    project_id = await enabled_project(conn, plugin, project)
    c = _T["canvas_claims"]
    query = select(c).where(c.c.project_id == project_id, c.c.archived_at.is_(None))
    if canvas is not None:
        query = query.where(c.c.canvas_id == canvas)
    if statuses:
        query = query.where(c.c.status.in_(list(statuses)))
    rows = (await conn.execute(query.order_by(c.c.canvas_id, c.c.cell, c.c.position).limit(MAX_ROWS))).all()
    return tuple(
        ClaimRef(
            r.id, r.canvas_id, r.number, r.cell, r.text, r.status, r.confidence, r.rev, _frozen(r.label), r.updated_at
        )
        for r in rows
    )


# Tasks


async def read_tasks(
    conn: AsyncConnection,
    plugin: Plugin,
    project: str,
    *,
    sprint: int | None = None,
    statuses: Sequence[str] = (),
    limit: int = 500,
) -> tuple[TaskRef, ...]:
    """The tasks of ``project``, last updated first: in sprint id ``sprint``, with one of ``statuses``."""
    project_id = await enabled_project(conn, plugin, project)
    t = _T["tasks_items"]
    query = select(t).where(t.c.project_id == project_id)
    if sprint is not None:
        query = query.where(t.c.sprint_id == sprint)
    if statuses:
        query = query.where(t.c.status.in_(list(statuses)))
    rows = (await conn.execute(query.order_by(t.c.updated_at.desc(), t.c.id.desc()).limit(_limit(limit)))).all()
    return tuple(
        TaskRef(
            r.id,
            r.number,
            r.type,
            r.title,
            r.status,
            r.priority,
            r.due_on,
            r.sprint_id,
            r.parent_id,
            _frozen(r.label),
            r.updated_at,
        )
        for r in rows
    )


async def read_sprints(conn: AsyncConnection, plugin: Plugin, project: str) -> tuple[SprintRef, ...]:
    """The sprints of ``project``, newest first."""
    project_id = await enabled_project(conn, plugin, project)
    s = _T["tasks_sprints"]
    rows = (await conn.execute(select(s).where(s.c.project_id == project_id).order_by(s.c.id.desc()))).all()
    return tuple(SprintRef(r.id, r.name, r.goal, r.state, r.starts_on, r.ends_on) for r in rows)


async def read_task_links(
    conn: AsyncConnection, plugin: Plugin, project: str, task_ids: Iterable[int]
) -> tuple[TaskLinkRef, ...]:
    """The links of the tasks of ``project`` among ``task_ids``, by task."""
    project_id = await enabled_project(conn, plugin, project)
    ids = list(task_ids)
    if not ids:
        return ()
    t, link = _T["tasks_items"], _T["tasks_links"]
    query = (
        select(link)
        .join_from(link, t, t.c.id == link.c.task_id)
        .where(t.c.project_id == project_id, link.c.task_id.in_(ids))
        .order_by(link.c.task_id, link.c.id)
        .limit(MAX_ROWS)
    )
    rows = (await conn.execute(query)).all()
    return tuple(TaskLinkRef(r.task_id, r.kind, r.srs_item_id, r.plan_id, r.step_id, r.run_id, r.url) for r in rows)


# SRS


async def _document(conn: AsyncConnection, project_id: int, project: str, slug: str) -> int:
    d = _T["srs_documents"]
    found = (
        await conn.execute(select(d.c.id).where(d.c.project_id == project_id, d.c.slug == slug))
    ).scalar_one_or_none()
    if found is None:
        raise Refused(404, NO_DOCUMENT.format(project=project, document=slug))
    return found


async def read_srs_items(
    conn: AsyncConnection, plugin: Plugin, project: str, *, document: str | None = None
) -> tuple[SrsItemRef, ...]:
    """The items of ``project``'s SRS documents, of the document of slug ``document`` only, by document and key."""
    project_id = await enabled_project(conn, plugin, project)
    i, d = _T["srs_items"], _T["srs_documents"]
    query = select(i, d.c.slug).join_from(i, d, d.c.id == i.c.document_id).where(i.c.project_id == project_id)
    if document is not None:
        query = query.where(i.c.document_id == await _document(conn, project_id, project, document))
    rows = (await conn.execute(query.order_by(d.c.slug, i.c.key).limit(MAX_ROWS))).all()
    return tuple(SrsItemRef(r.id, r.slug, r.key, r.kind, r.code, r.created_in, r.retired_in) for r in rows)


async def read_srs_versions(
    conn: AsyncConnection, plugin: Plugin, project: str, document: str
) -> tuple[SrsVersionRef, ...]:
    """The versions of SRS document ``document`` of ``project``, newest first; 404 without it."""
    project_id = await enabled_project(conn, plugin, project)
    document_id = await _document(conn, project_id, project, document)
    v = _T["srs_versions"]
    rows = (await conn.execute(select(v).where(v.c.document_id == document_id).order_by(v.c.number.desc()))).all()
    return tuple(SrsVersionRef(document, r.number, r.state, r.summary, r.created_at, r.decided_at) for r in rows)
