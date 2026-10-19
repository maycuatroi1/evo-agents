"""The tool of the plugin tasks on the hub's /mcp: ``tasks__create``, a task an agent makes for the member who asked
it, such as @ba making a task of a use case (criterion a7 of plan srs-tasks-plugins).

The hub lists it in a session whose project enabled the plugin, and checks its arguments against the manifest's
schema first. It acts for a person: the agent of a chat run for the person who mentioned the agent, the origin of the
chain (``chat_runs.ChatRun.dispatcher_id``), also when the run went to the worker of its agent type's run_as; a
member's own session (no run) for that member. The agent of any other kind of run gets a refusal: nobody asked it for
a task in a thread. The person must hold writer on the project when the tool runs. The task is theirs, carries the
project's label, takes the config's first type and status unless named, and links the SRS items it names by code
while the project enables srs, through the session's sink. A build of the project's knowledge graph is queued, as
after a task made with the API.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.tasks import store, writes
from evo_agents.core_plugins.tasks.settings import TasksError, settings
from evo_agents.hub import tables
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.server.chat_runs import chat_run
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import MACHINE, Principal

log = logging.getLogger(__name__)

RACE = "another task of the project took that number at the same moment: nothing was written; try again"


def _text(text: str, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": error}


async def _person(conn: AsyncConnection, params: dict, context: CoreContext) -> Principal:
    """The member the tool acts for (see the module); TasksError (403) for the agent of a run of another kind."""
    u, r = tables.users, tables.runs
    if context.run_id is None:
        login = (params.get("caller") or {}).get("login") or ""
        user_id = (await conn.execute(select(u.c.id).where(u.c.login == login))).scalar_one_or_none()
    else:
        found = await chat_run(conn, context.run_id)
        if found is None:
            kind = (await conn.execute(select(r.c.kind).where(r.c.id == context.run_id))).scalar_one()
            raise TasksError(
                403,
                f"the agent of a {kind} run creates no task: only the agent of a chat run does, for the member who "
                "mentioned it",
            )
        user_id = found.dispatcher_id
    if user_id is None:
        raise TasksError(403, "the caller is no member of the hub")
    login = (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()
    admin = context.config is not None and context.config.is_admin(login)
    return Principal(user_id, login, admin, None, MACHINE, "")


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
        assignees=list(args.get("assignees") or ()),
        roles=roles,
        links=writes.LinksIn(use_cases=list(args.get("links") or ())),
    )


async def _create(conn: AsyncConnection, params: dict, context: CoreContext) -> str:
    person = await _person(conn, params, context)
    access = await _access(conn, person, context.project)
    store.writer(access, "creating a task")
    config = await settings(conn, access.project_id, access.name)
    writer = writes.Writer(access, person.user_id, config, context.sink)
    task = _task(params.get("arguments") or {})
    number = await writes.create(conn, writer, task)
    if context.jobs is not None:  # the knowledge graph's tickets are the tasks
        await queue_if_configured(context.jobs, conn, access.project_id, access.name)
    ref = config.ref(number)
    linked = f", linked to {', '.join(task.links.use_cases)}" if task.links.use_cases else ""
    log.info("task created by an agent", extra={"project": access.name, "task": ref, "run_id": context.run_id})
    return (
        f"Task {ref} created for {person.login}{linked}: {task.title}. Its page: /p/{access.name}/x/tasks/board/{ref}"
    )


async def create(params: dict, context: CoreContext) -> dict:
    """The tool ``tasks__create``: a new task for the member the agent works for."""
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
