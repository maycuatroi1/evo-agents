"""The tool of the plugin tasks on the hub's /mcp: ``tasks__create``, a task an agent makes for the member who asked
it, such as @ba making a task of a use case (criterion a7 of plan srs-tasks-plugins), or a child task of a frame task an
approval made (criterion a8 of plan srs-conflicts-and-tasks).

The hub lists it in a session whose project enabled the plugin, and checks its arguments against the manifest's
schema first. It acts for a person: the agent of a chat run for the person who mentioned the agent, the origin of the
chain (``chat_runs.ChatRun.dispatcher_id``), also when the run went to the worker of its agent type's run_as; the agent
of the task run of @ba that splits the frame tasks of an approved SRS version (``core_plugins.srs.auto_tasks``) for
the member whose approval made the version approved, whom the hub keeps for that run (srs_approval_tasks.approved_by,
by split_run_id, the task's first run, which a lost attempt's next goes on from); a member's own session (no run) for
that member. The agent of any other run gets a refusal: nobody asked it for a task. The person must hold writer on
the project when the tool runs. The task is theirs, carries the project's label, takes the config's first type and
status unless named, and links the SRS items it names by code while the project enables srs, through the session's
sink. A build of the project's knowledge graph is queued, as after a task made with the API.

``parent`` puts the task under another task of the project, as T-12. A child whose title another child of the same
parent has, compared with case and runs of whitespace folded, is refused, so a split run tried again makes no twin; the
parent's row is locked first, so two calls cannot both pass. The split run always names a parent, and only a frame
task (an external_key that starts with ``tables_srs.FRAME_TASK_PREFIX``); its child tasks link the use cases the frame
task links too, besides those the call names.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.tasks import links, store, writes
from evo_agents.core_plugins.tasks.settings import Settings, TasksError, number_of, settings
from evo_agents.hub import tables
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.server.chat_runs import chat_run
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import MACHINE, Principal
from evo_agents.hub.tables_srs import FRAME_TASK_PREFIX
from evo_agents.hub.task_runs import task_of

log = logging.getLogger(__name__)

RACE = "another task of the project took that number at the same moment: nothing was written; try again"


def _text(text: str, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": error}


async def _approver(conn: AsyncConnection, run_id: int) -> int | None:
    """The member task run ``run_id`` acts for when it is the run of @ba that splits the frame tasks of an approved
    SRS version (or the next attempt of one): who approved that version; None for any other run."""
    r, a = tables.runs, tables.srs.approval_tasks
    found = (await conn.execute(select(r.c.kind, r.c.branch).where(r.c.id == run_id))).first()
    if found is None or found.kind != "task":
        return None
    first = task_of(found.branch) or run_id
    return await conn.scalar(select(a.c.approved_by).where(a.c.split_run_id == first))


async def _person(conn: AsyncConnection, params: dict, context: CoreContext) -> tuple[Principal, bool]:
    """(the member the tool acts for, whether the caller is the run that splits frame tasks) (see the module);
    TasksError (403) for the agent of a run of another kind."""
    u, r = tables.users, tables.runs
    splitting = False
    if context.run_id is None:
        login = (params.get("caller") or {}).get("login") or ""
        user_id = (await conn.execute(select(u.c.id).where(u.c.login == login))).scalar_one_or_none()
    else:
        found = await chat_run(conn, context.run_id)
        if found is not None:
            user_id = found.dispatcher_id
        else:
            user_id, splitting = await _approver(conn, context.run_id), True
            if user_id is None:
                kind = (await conn.execute(select(r.c.kind).where(r.c.id == context.run_id))).scalar_one()
                raise TasksError(
                    403,
                    f"the agent of a {kind} run creates no task: only the agent of a chat run does, for the member "
                    "who mentioned it, and the run of @ba that splits the frame tasks of an approved SRS version, for "
                    "the member who approved it",
                )
    if user_id is None:
        raise TasksError(403, "the caller is no member of the hub")
    login = (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()
    admin = context.config is not None and context.config.is_admin(login)
    return Principal(user_id, login, admin, None, MACHINE, ""), splitting


async def _access(conn: AsyncConnection, person: Principal, project: str) -> ProjectAccess:
    try:
        return await project_access(conn, person, project)
    except HTTPException:
        raise TasksError(403, f"{person.login} holds no grant on project {project} now") from None


def _task(args: dict) -> writes.TaskInput:
    roles = {role: args[role] for role in ("ba", "qa", "dev") if args.get(role)}
    return writes.TaskInput(
        title=args["title"],
        description_md=args.get("description_md") or "",
        type=args.get("type"),
        status=args.get("status"),
        priority=args.get("priority") or "medium",
        sprint=args.get("sprint"),
        parent=args.get("parent"),
        assignees=list(args.get("assignees") or ()),
        roles=roles,
        links=writes.LinksIn(use_cases=list(args.get("links") or ())),
    )


def _folded(title: str) -> str:
    return " ".join(title.split()).casefold()


async def _parent(conn: AsyncConnection, writer: writes.Writer, task: writes.TaskInput, splitting: bool):
    """The row of the task ``task.parent`` names, locked, after the checks of the module; None for no parent, or for
    one the member does not see, which ``writes.create`` refuses as it refuses any."""
    if task.parent is None:
        if splitting:
            raise TasksError(422, "the run that splits frame tasks makes child tasks: name the frame task in parent")
        return None
    t, access = tables.tasks.items, writer.access
    query = select(t.c.id, t.c.label, t.c.external_key).where(
        t.c.project_id == access.project_id, t.c.number == number_of(task.parent)
    )
    row = (await conn.execute(query.with_for_update())).first()
    if row is None or not store.visible(access, row.label, writer.sink):
        return None
    if splitting and not (row.external_key or "").startswith(FRAME_TASK_PREFIX):
        raise TasksError(
            422,
            f"{task.parent} is no frame task of an approved SRS version: the run that splits frame tasks makes child "
            "tasks of those alone",
        )
    children = await conn.execute(select(t.c.number, t.c.title).where(t.c.parent_id == row.id))
    twin = next((child.number for child in children if _folded(child.title) == _folded(task.title)), None)
    if twin is not None:
        ref = writer.settings.ref(twin)
        raise TasksError(
            409, f"{task.parent} has a child task {ref} titled {task.title!r} already: nothing was written"
        )
    return row


async def _create(conn: AsyncConnection, params: dict, context: CoreContext) -> str:
    person, splitting = await _person(conn, params, context)
    access = await _access(conn, person, context.project)
    store.writer(access, "creating a task")
    config: Settings = await settings(conn, access.project_id, access.name)
    writer = writes.Writer(access, person.user_id, config, context.sink)
    task = _task(params.get("arguments") or {})
    parent = await _parent(conn, writer, task, splitting)
    if splitting and parent is not None:  # a child task links its frame task's use cases
        held = (await links.codes(conn, access, context.sink, [parent.id]))[parent.id]
        task.links.use_cases = list(dict.fromkeys([*task.links.use_cases, *held]))
    number = await writes.create(conn, writer, task)
    if context.jobs is not None:  # the knowledge graph's tickets are the tasks
        await queue_if_configured(context.jobs, conn, access.project_id, access.name)
    ref = config.ref(number)
    linked = f", linked to {', '.join(task.links.use_cases)}" if task.links.use_cases else ""
    under = f", a child of {config.ref(number_of(task.parent))}" if task.parent else ""
    log.info("task created by an agent", extra={"project": access.name, "task": ref, "run_id": context.run_id})
    return (
        f"Task {ref} created for {person.login}{linked}{under}: {task.title}. "
        f"Its page: /p/{access.name}/x/tasks/board/{ref}"
    )


async def create(params: dict, context: CoreContext) -> dict:
    """The tool ``tasks__create``: a new task for the member the agent works for, under ``parent`` when named."""
    try:
        async with context.engine.begin() as conn:
            return _text(await _create(conn, params, context))
    except TasksError as exc:
        return _text(f"error: {exc.message}", error=True)
    except HTTPException as exc:
        return _text(f"error: {exc.detail}", error=True)
    except IntegrityError:
        return _text(f"error: {RACE}", error=True)


__all__ = ["create"]
