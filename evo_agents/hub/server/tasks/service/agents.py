"""The task an agent makes through the tool ``tasks__create`` (``agent``): the member it acts for (``person``), their
access, the parent the call names, checked as the tool's module says, and the task made (``create``).
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.server.chats.service.runs import chat_run
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import Principal, member_principal
from evo_agents.hub.server.tasks.models.values import (
    LinksIn,
    Settings,
    TaskInput,
    TasksError,
    Writer,
    number_of,
    visible,
)
from evo_agents.hub.server.tasks.repository import runs
from evo_agents.hub.server.tasks.repository import tasks as task_rows
from evo_agents.hub.server.tasks.service import links, writes
from evo_agents.hub.server.tasks.service.project import graph_changed, settings, writer
from evo_agents.hub.tables_srs import FRAME_TASK_PREFIX
from evo_agents.hub.task_runs import task_of


@dataclass(frozen=True)
class Made:
    """A task an agent made: who for, where, its ref, and the text the tool answers."""

    project: str
    ref: str
    text: str


async def _approver(conn: AsyncConnection, run_id: int) -> int | None:
    """The member task run ``run_id`` acts for when it is the run of @ba that splits the frame tasks of an approved
    SRS version or use case (or the next attempt of one): who approved that version, or that use case
    (srs_use_case_releases.released_by, by the split run its ``tasks`` keeps); None for any other run."""
    found = await runs.run(conn, run_id)
    if found is None or found.kind != "task":
        return None
    return await runs.approver_of(conn, task_of(found.branch) or run_id)


async def person(conn: AsyncConnection, params: dict, context: CoreContext) -> tuple[Principal, bool]:
    """(the member the tool acts for, whether the caller is the run that splits frame tasks) (see ``agent``);
    TasksError (403) for the agent of a run of another kind."""
    splitting = False
    if context.run_id is None:
        login = (params.get("caller") or {}).get("login") or ""
        user_id = await runs.user_id(conn, login)
    else:
        found = await chat_run(conn, context.run_id)
        if found is not None:
            user_id = found.dispatcher_id
        else:
            user_id, splitting = await _approver(conn, context.run_id), True
            if user_id is None:
                kind = await runs.run_kind(conn, context.run_id)
                raise TasksError(
                    403,
                    f"the agent of a {kind} run creates no task: only the agent of a chat run does, for the member "
                    "who mentioned it, and the run of @ba that splits the frame tasks of an approved SRS version, for "
                    "the member who approved it",
                )
    if user_id is None:
        raise TasksError(403, "the caller is no member of the hub")
    return await member_principal(conn, user_id, context.config), splitting


async def _access(conn: AsyncConnection, person: Principal, project: str) -> ProjectAccess:
    try:
        return await project_access(conn, person, project)
    except HTTPException:
        raise TasksError(403, f"{person.login} holds no grant on project {project} now") from None


def _task(args: dict) -> TaskInput:
    roles = {role: args[role] for role in ("ba", "qa", "dev") if args.get(role)}
    return TaskInput(
        title=args["title"],
        description_md=args.get("description_md") or "",
        type=args.get("type"),
        status=args.get("status"),
        priority=args.get("priority") or "medium",
        sprint=args.get("sprint"),
        parent=args.get("parent"),
        assignees=list(args.get("assignees") or ()),
        roles=roles,
        links=LinksIn(use_cases=list(args.get("links") or ())),
    )


def _folded(title: str) -> str:
    return " ".join(title.split()).casefold()


async def _parent(conn: AsyncConnection, writer: Writer, task: TaskInput, splitting: bool):
    """The row of the task ``task.parent`` names, locked, after the checks of ``agent``; None for no parent, or for
    one the member does not see, which ``writes.create`` refuses as it refuses any."""
    if task.parent is None:
        if splitting:
            raise TasksError(422, "the run that splits frame tasks makes child tasks: name the frame task in parent")
        return None
    access = writer.access
    row = await task_rows.task_label(conn, access.project_id, number_of(task.parent), lock=True)
    if row is None or not visible(access, row.label, writer.sink):
        return None
    if splitting and not (row.external_key or "").startswith(FRAME_TASK_PREFIX):
        raise TasksError(
            422,
            f"{task.parent} is no frame task of an approved SRS version: the run that splits frame tasks makes child "
            "tasks of those alone",
        )
    children = await task_rows.children(conn, row.id)
    twin = next((child.number for child in children if _folded(child.title) == _folded(task.title)), None)
    if twin is not None:
        ref = writer.settings.ref(twin)
        raise TasksError(
            409, f"{task.parent} has a child task {ref} titled {task.title!r} already: nothing was written"
        )
    return row


async def create(conn: AsyncConnection, params: dict, context: CoreContext) -> Made:
    """A new task for the member the agent works for, under ``parent`` when named (see ``agent``)."""
    member, splitting = await person(conn, params, context)
    access = await _access(conn, member, context.project or "")
    writer(access, "creating a task")
    config: Settings = await settings(conn, access.project_id, access.name)
    as_member = Writer(access, member.user_id, config, context.sink)
    task = _task(params.get("arguments") or {})
    parent = await _parent(conn, as_member, task, splitting)
    if splitting and parent is not None:  # a child task links its frame task's use cases
        held = (await links.codes(conn, access, context.sink, [parent.id]))[parent.id]
        task.links.use_cases = list(dict.fromkeys([*task.links.use_cases, *held]))
    number = await writes.create(conn, as_member, task)
    if context.jobs is not None:  # the knowledge graph's tickets are the tasks
        await graph_changed(context.jobs, conn, access)
    ref = config.ref(number)
    linked = f", linked to {', '.join(task.links.use_cases)}" if task.links.use_cases else ""
    under = f", a child of {config.ref(number_of(task.parent))}" if task.parent else ""
    text = (
        f"Task {ref} created for {member.login}{linked}{under}: {task.title}. "
        f"Its page: /p/{access.name}/x/tasks/board/{ref}"
    )
    return Made(access.name, ref, text)


__all__ = ["Made", "create", "person"]
