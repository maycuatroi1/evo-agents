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

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from types import MappingProxyType

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import signals
from evo_agents.hub.plans import step_key as plan_step_key
from evo_agents.hub.plugin_calls import ApiContext, enabled_in
from evo_agents.hub.server.chats.service.access import agent_projects
from evo_agents.hub.server.chats.service.runs import chat_run, hub_reply
from evo_agents.hub.server.core_plugin_api.models.reads import MAX_ROWS
from evo_agents.hub.server.core_plugin_api.models.tasks import (
    RULES,
    PlanStepChangeRef,
    SprintDetailRef,
    TaskActivityRef,
    TaskBlockerRef,
    TaskDecisionRef,
    TaskDetailRef,
    TaskEventRef,
    TaskPersonRef,
    TaskSrsItemRef,
    ToolCaller,
    Visibility,
)
from evo_agents.hub.server.core_plugin_api.models.types import Plugin, ProjectConfig, Refused
from evo_agents.hub.server.core_plugin_api.repository import tasks as rows_of
from evo_agents.hub.server.core_plugin_api.service.ground import project_grants
from evo_agents.hub.server.core_plugin_api.service.handles import issue_session, own_plugin
from evo_agents.hub.server.core_plugin_api.service.reads import enabled_project
from evo_agents.hub.server.plugins.service.configs import stored_stacks
from evo_agents.hub.server.security import member_principal
from evo_agents.hub.tables_srs import FRAME_TASK_PREFIX
from evo_agents.plugins import config as plugin_config

NO_CHATS_WRITE = "project {project} does not grant plugin {plugin} chats:write"


def _frozen(value) -> MappingProxyType:
    return MappingProxyType(dict(value or {}))


def _ids(task_ids: Iterable[int]) -> list[int]:
    ids = list(dict.fromkeys(task_ids))
    if len(ids) > MAX_ROWS:
        raise ValueError(f"a read names at most {MAX_ROWS} tasks")
    return ids


# Tasks and sprints


async def read_task_details(
    conn: AsyncConnection, plugin: Plugin, project: str, *, limit: int = MAX_ROWS
) -> tuple[TaskDetailRef, ...]:
    """The tasks of ``project`` by number, each with its texts and its sprint's name."""
    project_id = await enabled_project(conn, plugin, project)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_ROWS:
        raise ValueError(f"a read's limit is from 1 to {MAX_ROWS}")
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
        for r in await rows_of.task_details(conn, project_id, limit)
    )


async def read_sprint_details(
    conn: AsyncConnection, plugin: Plugin, project: str, *, states: Sequence[str] = ()
) -> tuple[SprintDetailRef, ...]:
    """The sprints of ``project``, newest first, with their times: in one of ``states``."""
    project_id = await enabled_project(conn, plugin, project)
    rows = await rows_of.sprint_details(conn, project_id, states)
    return tuple(
        SprintDetailRef(
            r.id, r.name, r.goal, r.state, r.starts_on, r.ends_on, r.created_at, r.activated_at, r.closed_at
        )
        for r in rows
    )


# What each task shows of the work on it


async def read_task_people(
    conn: AsyncConnection, plugin: Plugin, project: str, task_ids: Iterable[int], *, roles: Sequence[str] = ()
) -> tuple[TaskPersonRef, ...]:
    """The people of the tasks of ``project`` among ``task_ids``, by task and login: in one of ``roles``."""
    project_id = await enabled_project(conn, plugin, project)
    ids = _ids(task_ids)
    if not ids:
        return ()
    rows = await rows_of.task_people(conn, project_id, ids, roles)
    return tuple(TaskPersonRef(r.task_id, r.role, r.user_id, r.login) for r in rows)


async def read_task_events(
    conn: AsyncConnection, plugin: Plugin, project: str, task_ids: Iterable[int]
) -> tuple[TaskEventRef, ...]:
    """The events of the tasks of ``project`` among ``task_ids`` (what changed of each field), oldest first."""
    project_id = await enabled_project(conn, plugin, project)
    ids = _ids(task_ids)
    if not ids:
        return ()
    rows = await rows_of.task_events(conn, project_id, ids)
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
    commented = await rows_of.last_comments(conn, project_id, ids)
    ran = await rows_of.last_runs(conn, project_id, ids)
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
    return tuple(
        TaskBlockerRef(r.id, r.task_id, r.reason, r.since, r.set_by, r.waiting_login, r.waiting_number, r.waiting_text)
        for r in await rows_of.task_blockers(conn, project_id, ids)
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
    return tuple(
        TaskDecisionRef(r.task_id, r.decision_id, r.run_id, r.question, r.state, r.asked_at, r.login)
        for r in await rows_of.task_decisions(conn, project_id, ids, states)
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
    return tuple(
        TaskSrsItemRef(r.task_id, r.id, r.slug, r.kind, r.code, r.title, r.retired_in, _frozen(r.label))
        for r in await rows_of.task_srs_items(conn, project_id, ids)
    )


# Plan steps across revisions


def _statuses(body) -> dict[str, str | None]:
    """step key -> its status, for each step of a plan's body that is a mapping (the first one of a key)."""
    steps = body.get("steps") if isinstance(body, dict) and isinstance(body.get("steps"), list) else []
    found: dict[str, str | None] = {}
    for index, item in enumerate(steps):  # pyright: ignore[reportArgumentType]
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
    found: dict[str, list] = {}
    for row in await rows_of.plan_revisions(conn, project_id, wanted, since):
        found.setdefault(row.plan_id, []).append(row)
    changes = []
    for plan_id, rows in found.items():
        before = await rows_of.plan_revision(conn, project_id, plan_id, rows[0].revision - 1)
        chain = [*before, *rows]
        for older, newer in zip(chain, chain[1:], strict=False):
            was, now = _statuses(older.body), _statuses(newer.body)
            for key in sorted(set(was) | set(now)):
                if was.get(key) != now.get(key):
                    changes.append(
                        PlanStepChangeRef(plan_id, key, newer.revision, newer.created_at, was.get(key), now.get(key))
                    )
    return tuple(changes)


# How a reader sees labelled data


async def read_visibility(
    conn: AsyncConnection, plugin: Plugin, project: str, *, max_level: str | None, sink: str | None = None
) -> Visibility:
    """How a reader whose grant on ``project`` reaches ``max_level`` (``ProjectRef.max_level``; None sees nothing
    labelled above none) sees its data through ``sink``, else through the project's hub sink."""
    project_id = await enabled_project(conn, plugin, project)
    rules = await signals.project_rules(conn, project_id)
    through = sink or (rules.hub_sink.sink if rules.hub_sink else None)
    made = Visibility(project, tuple(rules.levels), max_level, through)
    RULES[made] = rules
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
    if not await rows_of.chat_exists(conn, chat_id):
        return None
    if not any(found.id == project_id for found in await agent_projects(conn, chat_id)):
        return None
    return await hub_reply(conn, chat_id, None, text)


async def tool_caller(conn: AsyncConnection, plugin: Plugin, context: ApiContext, params: Mapping) -> ToolCaller:
    """The member a call of one of the plugin's tools (``context`` and ``params`` as the hub gave them) acts for, as
    a ``Session`` that ``project_access`` checks as the hub checks that member: for the agent of a chat run, the
    member at the origin of its chain; for the agent of a run of another kind, nobody; for a member's own session on
    /mcp, that member. A hub admin of the hub's config passes as admin, as on the hub's routes."""
    own_plugin(plugin)
    if not isinstance(context, ApiContext):
        raise ValueError("a tool's context is the one the hub gave it")
    if context.run_id is None:
        login = ((params or {}).get("caller") or {}).get("login") or ""
        user_id = await rows_of.user_id_of(conn, login)
        run_kind = None
    else:
        found = await chat_run(conn, context.run_id)
        if found is None:
            kind = await rows_of.run_kind(conn, context.run_id)
            return ToolCaller(None, context.run_id, kind)
        user_id, run_kind = found.dispatcher_id, "chat"
    if user_id is None:
        return ToolCaller(None, context.run_id, run_kind)
    config = context.config if hasattr(context.config, "is_admin") else None
    principal = await member_principal(conn, user_id, config)
    admin, login = principal.admin, principal.login
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
