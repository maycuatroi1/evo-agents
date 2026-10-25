"""Reads and helpers of API 1.3 added while the core plugins of evo-agents moved onto the API (plan plugin-market,
step 5). Private to ``core_plugin_api``, which exports each name of ``__all__``.

The work on a project's tasks as the hub keeps it: each task with its texts, its sprint and its labels, the sprints
with their times, the people of each task, its events, its latest comment and run, its open blockers and the open
decisions of its runs, the SRS items it links with their current title, and the changes of the status of plan steps
from one revision of a plan to the next. Beside them, how a reader sees labelled data (``read_visibility``), the
config of another plugin a project enables (``enabled_plugin_config``), a message of the hub in a group chat
(``post_in_chat``) and the member a call of one of the plugin's tools acts for (``tool_caller``). Each read names a
project that enables the plugin (404 otherwise) and keeps to its tasks, whatever ids it is given."""

from __future__ import annotations

import weakref
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import signals, tables
from evo_agents.hub.access import ProjectRules
from evo_agents.hub.plans import step_key as plan_step_key
from evo_agents.hub.plugin_calls import ApiContext, enabled_in
from evo_agents.hub.server.chat_access import agent_projects
from evo_agents.hub.server.chat_runs import chat_run, hub_reply
from evo_agents.hub.server.core_plugin_api._base import project_grants
from evo_agents.hub.server.core_plugin_api._reads import MAX_ROWS, enabled_project
from evo_agents.hub.server.core_plugin_api._types import (
    Plugin,
    ProjectConfig,
    Refused,
    Session,
    issue_session,
    own_plugin,
)
from evo_agents.hub.server.plugin_configs import stored_stacks
from evo_agents.hub.server.security import MACHINE, Principal
from evo_agents.hub.tables_srs import FRAME_TASK_PREFIX
from evo_agents.plugins import config as plugin_config

_T = tables.metadata.tables
NO_CHATS_WRITE = "project {project} does not grant plugin {plugin} chats:write"


def _frozen(value) -> MappingProxyType:
    return MappingProxyType(dict(value or {}))


def _ids(task_ids: Iterable[int]) -> list[int]:
    ids = list(dict.fromkeys(task_ids))
    if len(ids) > MAX_ROWS:
        raise ValueError(f"a read names at most {MAX_ROWS} tasks")
    return ids


def _of_project(query, task_id, project_id: int):
    """``query`` kept to the tasks of project ``project_id``, joined on ``task_id``."""
    t = _T["tasks_items"]
    return query.join(t, t.c.id == task_id).where(t.c.project_id == project_id)


# Tasks and sprints


@dataclass(frozen=True)
class TaskDetailRef:
    """A task with what ``TaskRef`` leaves out: its texts, its sprint's name, the key of what made it outside the
    hub and whether it is a task of an SRS frame."""

    id: int
    number: int
    type: str
    title: str
    status: str
    priority: str | None
    due_on: Any  # a date, or None
    sprint_id: int | None
    sprint: str | None  # the sprint's name
    parent_id: int | None
    description_md: str | None
    outcome_md: str | None
    external_key: str | None
    frame: bool  # made by the frame of an SRS document
    label: Mapping[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SprintDetailRef:
    """A sprint with its times: made, activated and closed."""

    id: int
    name: str
    goal: str | None
    state: str
    starts_on: Any
    ends_on: Any
    created_at: datetime
    activated_at: datetime | None
    closed_at: datetime | None


async def read_task_details(
    conn: AsyncConnection, plugin: Plugin, project: str, *, limit: int = MAX_ROWS
) -> tuple[TaskDetailRef, ...]:
    """The tasks of ``project`` by number, each with its texts and its sprint's name."""
    project_id = await enabled_project(conn, plugin, project)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_ROWS:
        raise ValueError(f"a read's limit is from 1 to {MAX_ROWS}")
    t, s = _T["tasks_items"], _T["tasks_sprints"]
    query = (
        select(t, s.c.name.label("sprint"))
        .select_from(t.outerjoin(s, s.c.id == t.c.sprint_id))
        .where(t.c.project_id == project_id)
        .order_by(t.c.number)
        .limit(limit)
    )
    return tuple(
        TaskDetailRef(
            r.id,
            r.number,
            r.type,
            r.title,
            r.status,
            r.priority,
            r.due_on,
            r.sprint_id,
            r.sprint,
            r.parent_id,
            r.description_md,
            r.outcome_md,
            r.external_key,
            (r.external_key or "").startswith(FRAME_TASK_PREFIX),
            _frozen(r.label),
            r.created_at,
            r.updated_at,
        )
        for r in (await conn.execute(query)).all()
    )


async def read_sprint_details(
    conn: AsyncConnection, plugin: Plugin, project: str, *, states: Sequence[str] = ()
) -> tuple[SprintDetailRef, ...]:
    """The sprints of ``project``, newest first, with their times: in one of ``states``."""
    project_id = await enabled_project(conn, plugin, project)
    s = _T["tasks_sprints"]
    query = select(s).where(s.c.project_id == project_id)
    if states:
        query = query.where(s.c.state.in_(list(states)))
    rows = (await conn.execute(query.order_by(s.c.id.desc()))).all()
    return tuple(
        SprintDetailRef(
            r.id, r.name, r.goal, r.state, r.starts_on, r.ends_on, r.created_at, r.activated_at, r.closed_at
        )
        for r in rows
    )


# What each task shows of the work on it


@dataclass(frozen=True)
class TaskPersonRef:
    task_id: int
    role: str  # assignee, qa, ... as the plugin tasks names them
    user_id: int
    login: str


@dataclass(frozen=True)
class TaskEventRef:
    id: int
    task_id: int
    field: str
    from_value: str | None
    to_value: str | None
    at: datetime


@dataclass(frozen=True)
class TaskActivityRef:
    """The latest comment on a task and the latest move of a run of it: one it links, or one of a plan step it
    links (queued, leased, started or finished)."""

    task_id: int
    commented_at: datetime | None
    run_at: datetime | None


@dataclass(frozen=True)
class TaskBlockerRef:
    """An open blocker of a task, set by hand: what it waits on is a member, a task (by number) or a text."""

    id: int
    task_id: int
    reason: str
    since: datetime
    set_by: str  # the login of who set it
    waiting_login: str | None
    waiting_task_number: int | None
    waiting_text: str | None


@dataclass(frozen=True)
class TaskDecisionRef:
    """A decision of a run of a task (one it links, or one of a plan step it links), and the member who
    dispatched the run."""

    task_id: int
    decision_id: int
    run_id: int
    question: str
    state: str
    asked_at: datetime
    dispatched_by: str  # their login


@dataclass(frozen=True)
class TaskSrsItemRef:
    """An SRS item a task links, with its title at the newest version of its document and its document's label."""

    task_id: int
    item_id: int
    document: str  # its document's slug
    kind: str
    code: str | None
    title: str | None
    retired_in: int | None
    label: Mapping[str, Any]  # its document's


async def read_task_people(
    conn: AsyncConnection, plugin: Plugin, project: str, task_ids: Iterable[int], *, roles: Sequence[str] = ()
) -> tuple[TaskPersonRef, ...]:
    """The people of the tasks of ``project`` among ``task_ids``, by task and login: in one of ``roles``."""
    project_id = await enabled_project(conn, plugin, project)
    ids = _ids(task_ids)
    if not ids:
        return ()
    p, u = _T["tasks_people"], tables.users
    query = _of_project(
        select(p.c.task_id, p.c.role, p.c.user_id, u.c.login).join_from(p, u, u.c.id == p.c.user_id),
        p.c.task_id,
        project_id,
    )
    query = query.where(p.c.task_id.in_(ids))
    if roles:
        query = query.where(p.c.role.in_(list(roles)))
    rows = (await conn.execute(query.order_by(p.c.task_id, u.c.login))).all()
    return tuple(TaskPersonRef(r.task_id, r.role, r.user_id, r.login) for r in rows)


async def read_task_events(
    conn: AsyncConnection, plugin: Plugin, project: str, task_ids: Iterable[int]
) -> tuple[TaskEventRef, ...]:
    """The events of the tasks of ``project`` among ``task_ids`` (what changed of each field), oldest first."""
    project_id = await enabled_project(conn, plugin, project)
    ids = _ids(task_ids)
    if not ids:
        return ()
    e = _T["tasks_events"]
    query = _of_project(
        select(e.c.id, e.c.task_id, e.c.field, e.c.from_value, e.c.to_value, e.c.at).select_from(e),
        e.c.task_id,
        project_id,
    )
    rows = (await conn.execute(query.where(e.c.task_id.in_(ids)).order_by(e.c.id))).all()
    return tuple(TaskEventRef(r.id, r.task_id, r.field, r.from_value, r.to_value, r.at) for r in rows)


async def read_task_activity(
    conn: AsyncConnection, plugin: Plugin, project: str, task_ids: Iterable[int]
) -> tuple[TaskActivityRef, ...]:
    """For each task of ``project`` among ``task_ids`` that has either, its latest comment and the latest move of a
    run of it, by task."""
    project_id = await enabled_project(conn, plugin, project)
    ids = _ids(task_ids)
    if not ids:
        return ()
    c, k, r = _T["tasks_comments"], _T["tasks_links"], tables.runs
    commented_query = _of_project(
        select(c.c.task_id, func.max(c.c.created_at).label("at")).select_from(c), c.c.task_id, project_id
    )
    commented = {
        row.task_id: row.at
        for row in await conn.execute(commented_query.where(c.c.task_id.in_(ids)).group_by(c.c.task_id))
    }
    moved = func.greatest(r.c.queued_at, r.c.leased_at, r.c.started_at, r.c.finished_at)
    by_run = and_(k.c.kind == "run", k.c.run_id == r.c.id)
    by_step = and_(k.c.kind == "plan_step", k.c.plan_id == r.c.plan_id, k.c.step_id == r.c.step_key)
    runs_query = _of_project(
        select(k.c.task_id, func.max(moved).label("at")).select_from(k.join(r, or_(by_run, by_step))),
        k.c.task_id,
        project_id,
    )
    ran = {
        row.task_id: row.at for row in await conn.execute(runs_query.where(k.c.task_id.in_(ids)).group_by(k.c.task_id))
    }
    return tuple(
        TaskActivityRef(task_id, commented.get(task_id), ran.get(task_id))
        for task_id in sorted(set(commented) | set(ran))
    )


async def read_task_blockers(
    conn: AsyncConnection, plugin: Plugin, project: str, task_ids: Iterable[int]
) -> tuple[TaskBlockerRef, ...]:
    """The open blockers set by hand on the tasks of ``project`` among ``task_ids``, oldest first."""
    project_id = await enabled_project(conn, plugin, project)
    ids = _ids(task_ids)
    if not ids:
        return ()
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
        .where(t.c.project_id == project_id, b.c.task_id.in_(ids), b.c.cleared_at.is_(None))
        .order_by(b.c.since, b.c.id)
    )
    return tuple(
        TaskBlockerRef(r.id, r.task_id, r.reason, r.since, r.set_by, r.waiting_login, r.waiting_number, r.waiting_text)
        for r in (await conn.execute(query)).all()
    )


async def read_task_decisions(
    conn: AsyncConnection,
    plugin: Plugin,
    project: str,
    task_ids: Iterable[int],
    *,
    states: Sequence[str] = ("open",),
) -> tuple[TaskDecisionRef, ...]:
    """The decisions of the runs of the tasks of ``project`` among ``task_ids`` (a run a task links, or one of a
    plan step it links), in one of ``states`` (all with none), oldest first."""
    project_id = await enabled_project(conn, plugin, project)
    ids = _ids(task_ids)
    if not ids:
        return ()
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
        .where(t.c.project_id == project_id, k.c.task_id.in_(ids))
        .distinct()
        .order_by(d.c.asked_at, d.c.id)
    )
    if states:
        query = query.where(d.c.state.in_(list(states)))
    return tuple(
        TaskDecisionRef(r.task_id, r.decision_id, r.run_id, r.question, r.state, r.asked_at, r.login)
        for r in (await conn.execute(query)).all()
    )


async def read_task_srs_items(
    conn: AsyncConnection, plugin: Plugin, project: str, task_ids: Iterable[int]
) -> tuple[TaskSrsItemRef, ...]:
    """The SRS items the tasks of ``project`` among ``task_ids`` link, by task and link, each with its title at the
    newest version of its document."""
    project_id = await enabled_project(conn, plugin, project)
    ids = _ids(task_ids)
    if not ids:
        return ()
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
        .where(i.c.project_id == project_id, k.c.task_id.in_(ids), k.c.kind == "srs_item")
        .order_by(k.c.task_id, k.c.id)
    )
    return tuple(
        TaskSrsItemRef(r.task_id, r.id, r.slug, r.kind, r.code, r.title, r.retired_in, _frozen(r.label))
        for r in (await conn.execute(query)).all()
    )


# Plan steps across revisions


@dataclass(frozen=True)
class PlanStepChangeRef:
    """The status of a plan's step changed from one revision to the next: ``revision`` made the change."""

    plan_id: str
    step_key: str
    revision: int
    changed_at: datetime  # when that revision was made
    from_status: str | None
    to_status: str | None


def _statuses(body) -> dict[str, str | None]:
    """step key -> its status, for each step of a plan's body that is a mapping (the first one of a key)."""
    steps = body.get("steps") if isinstance(body, dict) and isinstance(body.get("steps"), list) else []
    found: dict[str, str | None] = {}
    for index, item in enumerate(steps):
        if isinstance(item, dict):
            found.setdefault(plan_step_key(item, index), item.get("status"))
    return found


async def read_plan_step_changes(
    conn: AsyncConnection, plugin: Plugin, project: str, plans: Iterable[str], *, since: datetime
) -> tuple[PlanStepChangeRef, ...]:
    """The changes of the status of a step of the plans of ``project`` among ``plans``, made by a revision made at
    or after ``since`` against the revision before it, by plan, revision and step."""
    project_id = await enabled_project(conn, plugin, project)
    wanted = list(dict.fromkeys(plans))
    if not wanted:
        return ()
    rv = tables.plan_revisions
    columns = (rv.c.plan_id, rv.c.revision, rv.c.created_at, rv.c.body)
    query = select(*columns).where(rv.c.project_id == project_id, rv.c.plan_id.in_(wanted), rv.c.created_at >= since)
    found: dict[str, list] = {}
    for row in await conn.execute(query.order_by(rv.c.plan_id, rv.c.revision)):
        found.setdefault(row.plan_id, []).append(row)
    changes = []
    for plan_id, rows in found.items():
        before = await conn.execute(
            select(*columns).where(
                rv.c.project_id == project_id, rv.c.plan_id == plan_id, rv.c.revision == rows[0].revision - 1
            )
        )
        chain = [*before.all(), *rows]
        for older, newer in zip(chain, chain[1:], strict=False):
            was, now = _statuses(older.body), _statuses(newer.body)
            for key in sorted(set(was) | set(now)):
                if was.get(key) != now.get(key):
                    changes.append(
                        PlanStepChangeRef(plan_id, key, newer.revision, newer.created_at, was.get(key), now.get(key))
                    )
    return tuple(changes)


# How a reader sees labelled data


@dataclass(frozen=True, eq=False)
class Visibility:
    """How a reader with a grant up to ``max_level`` sees the labelled data of ``project`` through ``sink``: the one
    the read names, else the project's hub sink (None: neither, and nothing is visible). ``visible(label)`` is the
    read rule of the hub's routes for an object carrying ``label`` as stored."""

    project: str
    levels: tuple[str, ...]  # the project's ladder, lowest first
    max_level: str | None
    sink: str | None

    def visible(self, label) -> bool:
        rules = _RULES.get(self)
        if rules is None:
            raise ValueError("a Visibility comes from read_visibility only")
        return self.sink is not None and rules.visible(label, self.max_level, self.sink)


_RULES: weakref.WeakKeyDictionary[Visibility, ProjectRules] = weakref.WeakKeyDictionary()


async def read_visibility(
    conn: AsyncConnection, plugin: Plugin, project: str, *, max_level: str | None, sink: str | None = None
) -> Visibility:
    """How a reader whose grant on ``project`` reaches ``max_level`` (``ProjectRef.max_level``; None sees nothing
    labelled above none) sees its data through ``sink``, else through the project's hub sink."""
    project_id = await enabled_project(conn, plugin, project)
    rules = await signals.project_rules(conn, project_id)
    through = sink or (rules.hub_sink.sink if rules.hub_sink else None)
    made = Visibility(project, tuple(rules.levels), max_level, through)
    _RULES[made] = rules
    return made


# Another plugin's config, a message in a chat, the caller of a tool


async def enabled_plugin_config(conn: AsyncConnection, plugin: Plugin, project: str, name: str) -> ProjectConfig | None:
    """The effective config of plugin ``name`` in ``project``, its x-secret fields left out; None when the project
    does not enable it. What a plugin reads of the plugins it works beside, such as the statuses of tasks."""
    project_id = await enabled_project(conn, plugin, project)
    row = await enabled_in(conn, name, project_id)
    if row is None:
        return None
    layers = (await stored_stacks(conn, [row.id], project_id, None)).get(row.id, [])
    merged = plugin_config.merge(row.manifest.get("config"), layers)
    values = {field: item.value for field, item in merged.fields.items() if not item.secret}
    project_layer = next((layer for layer in layers if layer.name == "project"), None)
    return ProjectConfig(project, MappingProxyType(values), None if project_layer is None else project_layer.version)


async def post_in_chat(conn: AsyncConnection, plugin: Plugin, project: str, chat_id: int, text: str) -> int | None:
    """A message of the hub, starting no thread, in chat ``chat_id``, which an agent of ``project`` is in; its id,
    or None when the chat is gone or no agent of the project is in it any more. 403 unless the project grants the
    plugin ``chats:write``. ``text`` is cut as the hub cuts a message."""
    project_id = await enabled_project(conn, plugin, project)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("a message is a text that is not empty")
    if not await project_grants(conn, plugin.id, project, "chats:write"):
        raise Refused(403, NO_CHATS_WRITE.format(project=project, plugin=plugin.id))
    if await conn.scalar(select(tables.chats.c.id).where(tables.chats.c.id == chat_id)) is None:
        return None
    if not any(found.id == project_id for found in await agent_projects(conn, chat_id)):
        return None
    return await hub_reply(conn, chat_id, None, text)


@dataclass(frozen=True)
class ToolCaller:
    """Whom a call of one of the plugin's tools acts for: ``session`` is the member (None: nobody), and for the agent
    of a run, ``run_id`` and the run's kind."""

    session: Session | None
    run_id: int | None = None
    run_kind: str | None = None


async def tool_caller(conn: AsyncConnection, plugin: Plugin, context: ApiContext, params: Mapping) -> ToolCaller:
    """The member a call of one of the plugin's tools (``context`` and ``params`` as the hub gave them) acts for, as
    a ``Session`` that ``project_access`` checks as the hub checks that member: for the agent of a chat run, the
    member at the origin of its chain; for the agent of a run of another kind, nobody; for a member's own session on
    /mcp, that member. A hub admin of the hub's config passes as admin, as on the hub's routes."""
    own_plugin(plugin)
    if not isinstance(context, ApiContext):
        raise ValueError("a tool's context is the one the hub gave it")
    u = tables.users
    if context.run_id is None:
        login = ((params or {}).get("caller") or {}).get("login") or ""
        user_id = (await conn.execute(select(u.c.id).where(u.c.login == login))).scalar_one_or_none()
        run_kind = None
    else:
        found = await chat_run(conn, context.run_id)
        if found is None:
            r = tables.runs
            kind = (await conn.execute(select(r.c.kind).where(r.c.id == context.run_id))).scalar_one_or_none()
            return ToolCaller(None, context.run_id, kind)
        user_id, run_kind = found.dispatcher_id, "chat"
    if user_id is None:
        return ToolCaller(None, context.run_id, run_kind)
    login = (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()
    config = context.config
    admin = config is not None and hasattr(config, "is_admin") and bool(config.is_admin(login))
    principal = Principal(user_id, login, admin, None, MACHINE, "")
    session = issue_session(user_id, login, 0, False, admin=admin, principal=principal)
    return ToolCaller(session, context.run_id, run_kind)


__all__ = [
    "PlanStepChangeRef",
    "SprintDetailRef",
    "TaskActivityRef",
    "TaskBlockerRef",
    "TaskDecisionRef",
    "TaskDetailRef",
    "TaskEventRef",
    "TaskPersonRef",
    "TaskSrsItemRef",
    "ToolCaller",
    "Visibility",
    "enabled_plugin_config",
    "post_in_chat",
    "read_plan_step_changes",
    "read_sprint_details",
    "read_task_activity",
    "read_task_blockers",
    "read_task_decisions",
    "read_task_details",
    "read_task_events",
    "read_task_people",
    "read_task_srs_items",
    "read_visibility",
    "tool_caller",
]
