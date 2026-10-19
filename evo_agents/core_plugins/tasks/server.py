"""The routes of the plugin tasks, under /v1/x/tasks/<project>/ for a member with a grant on a project that enabled
it (the hub checks both first, ``evo_agents.hub.server.plugin_routes``; a project that did not enable it answers 404).

Readers read: the config as tasks are written against it, the tasks (by status, sprint, the caller's own or an
assignee's), a task whole with its links as they are now, its history and comments, the sprints. Writers create and
change tasks, link and unlink them, set their people, comment, keep sprints and import ``evo-tasks/1`` JSON once.
Reads go through the sink X-Evo-Sink names, else the project's hub sink. A refusal is the hub's error body, with an
import's problems in ``detail``. Creating, changing or importing tasks queues a build of the project's knowledge graph,
whose tickets they are.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Path, Query, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.tasks import imports, links, store, writes
from evo_agents.core_plugins.tasks.models import (
    TasksComment,
    TasksCommentIn,
    TasksImport,
    TasksImportResult,
    TasksItem,
    TasksItemDetail,
    TasksItemEdit,
    TasksItemIn,
    TasksSettings,
    TasksSprint,
    TasksSprintEdit,
    TasksSprintIn,
    comment_model,
    detail_model,
    import_model,
    item_model,
    settings_model,
    sprint_model,
)
from evo_agents.core_plugins.tasks.settings import Settings, TasksError, number_of, settings, srs_enabled
from evo_agents.hub.access import has_role
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser

REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 413, 422)}
RACE = "another write in the project took a number or a name at the same moment: nothing was written; try again"
ProjectName = Annotated[str, Path(pattern=PROJECT_NAME)]
Ref = Annotated[str, Path(min_length=1, max_length=20, description="the task's number, as T-42 or 42")]
SprintPath = Annotated[str, Path(min_length=1, max_length=100)]
Sink = Annotated[str | None, Header(alias=store.SINK_HEADER)]

router = APIRouter(tags=["plugins"], responses={401: {"model": ErrorBody}, **REFUSALS})


async def _answer(request: Request, work):
    """``work(conn)`` in one transaction; a refusal as the hub's error body, nothing written."""
    try:
        async with request.app.state.engine.begin() as conn:
            return await work(conn)
    except TasksError as exc:
        return error_response(request, exc.status, exc.message, detail=exc.detail)
    except IntegrityError:
        return error_response(request, 409, RACE)


async def _graph_changed(request: Request, conn: AsyncConnection, access: ProjectAccess) -> None:
    """Queue a build of the project's knowledge graph, whose tickets are the tasks (``kg``)."""
    await queue_if_configured(request.app.state.jobs, conn, access.project_id, access.name)


async def _opened(conn: AsyncConnection, user, project: str) -> tuple[ProjectAccess, Settings]:
    access = await project_access(conn, user, project)
    store.reader(access)
    return access, await settings(conn, access.project_id, access.name)


def _writer(access: ProjectAccess, user, sink: str | None, config: Settings, what: str) -> writes.Writer:
    store.writer(access, what)
    return writes.Writer(access, user.user_id, config, sink)


async def _items(conn, access: ProjectAccess, config: Settings, rows: list[store.TaskRow], sink) -> list[TasksItem]:
    ids = [row.id for row in rows]
    held = await store.people(conn, ids)
    done = await links.progress(conn, access, sink, ids)
    return [item_model(config, row, held[row.id], done[row.id]) for row in rows]


async def _detail(conn, access: ProjectAccess, config: Settings, number: int, sink) -> TasksItemDetail:
    row = await store.task(conn, access, number, sink)
    [item] = await _items(conn, access, config, [row], sink)
    shown = await links.shown(conn, access, sink, row.id)
    return detail_model(item, row, shown, await store.events(conn, row.id), await store.comments(conn, row.id))


# Reading


@router.get("/{project}/settings", response_model=TasksSettings)
async def show_settings(request: Request, project: ProjectName, user: CurrentUser):
    """The plugin's config in the project: the prefix, the statuses with their groups, the types."""

    async def work(conn):
        access, config = await _opened(conn, user, project)
        return settings_model(config, await srs_enabled(conn, access.project_id))

    return await _answer(request, work)


@router.get("/{project}/tasks", response_model=list[TasksItem])
async def list_tasks(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    status: Annotated[str | None, Query(max_length=40, description="a status key")] = None,
    sprint: Annotated[str | None, Query(max_length=100, description="a sprint's name")] = None,
    backlog: Annotated[bool, Query(description="the tasks of no sprint")] = False,
    mine: Annotated[bool, Query(description="the tasks the caller stands in a slot of")] = False,
    assignee: Annotated[str | None, Query(max_length=100, description="an assignee's login")] = None,
    limit: Annotated[int, Query(ge=1, le=store.MAX_LISTED)] = 500,
    sink: Sink = None,
):
    """The tasks of the project the caller sees, by number, that the filters keep."""

    async def work(conn):
        access, config = await _opened(conn, user, project)
        member = user.user_id if mine else None
        filters = store.Filters(status, sprint, backlog, member, assignee, limit)
        rows = await store.tasks(conn, access, sink, filters)
        return await _items(conn, access, config, rows, sink)

    return await _answer(request, work)


@router.get("/{project}/tasks/{ref}", response_model=TasksItemDetail)
async def show_task(request: Request, project: ProjectName, ref: Ref, user: CurrentUser, sink: Sink = None):
    """A task whole: its links as they are now (a plan step's status read from the plan), history and comments."""

    async def work(conn):
        access, config = await _opened(conn, user, project)
        return await _detail(conn, access, config, number_of(ref), sink)

    return await _answer(request, work)


@router.get("/{project}/sprints", response_model=list[TasksSprint])
async def list_sprints(request: Request, project: ProjectName, user: CurrentUser):
    """The sprints of the project, the newest first."""

    async def work(conn):
        access, _ = await _opened(conn, user, project)
        return [sprint_model(row) for row in await store.sprints(conn, access)]

    return await _answer(request, work)


# Writing


@router.post("/{project}/tasks", response_model=TasksItemDetail, status_code=201)
async def create_task(request: Request, project: ProjectName, body: TasksItemIn, user: CurrentUser, sink: Sink = None):
    """A new task, with its people and links."""

    async def work(conn):
        access, config = await _opened(conn, user, project)
        writer = _writer(access, user, sink, config, "creating a task")
        if body.label is not None and not has_role(access.role, "admin"):
            raise TasksError(403, f"giving a task a label of its own in project {access.name} takes an admin")
        given = body.model_dump(exclude={"roles", "links", "label"})
        task = writes.TaskInput(
            **given,
            roles=body.roles.given(),
            links=writes.LinksIn(**body.links.model_dump()),
            label=body.label.model_dump(exclude_none=True) if body.label else None,
        )
        number = await writes.create(conn, writer, task)
        await _graph_changed(request, conn, access)
        return await _detail(conn, access, config, number, sink)

    return await _answer(request, work)


@router.patch("/{project}/tasks/{ref}", response_model=TasksItemDetail)
async def edit_task(
    request: Request, project: ProjectName, ref: Ref, body: TasksItemEdit, user: CurrentUser, sink: Sink = None
):
    """Change a task: its fields, its people, its links."""

    async def work(conn):
        access, config = await _opened(conn, user, project)
        writer = _writer(access, user, sink, config, "changing a task")
        row = await store.task(conn, access, number_of(ref), sink, lock=True)
        changes = writes.TaskChanges(
            values=body.values(),
            assignees=body.assignees,
            roles=body.roles.given(),
            links=writes.LinksIn(**body.links.model_dump()),
            unlink=body.unlink,
        )
        await writes.edit(conn, writer, row, changes)
        await _graph_changed(request, conn, access)
        return await _detail(conn, access, config, row.number, sink)

    return await _answer(request, work)


@router.post("/{project}/tasks/{ref}/comments", response_model=TasksComment, status_code=201)
async def add_comment(
    request: Request, project: ProjectName, ref: Ref, body: TasksCommentIn, user: CurrentUser, sink: Sink = None
):
    """Comment on a task."""

    async def work(conn):
        access, config = await _opened(conn, user, project)
        writer = _writer(access, user, sink, config, "commenting on a task")
        row = await store.task(conn, access, number_of(ref), sink)
        comment_id = await writes.comment(conn, writer, row.id, body.body)
        return comment_model(next(c for c in await store.comments(conn, row.id) if c.id == comment_id))

    return await _answer(request, work)


@router.post("/{project}/sprints", response_model=TasksSprint, status_code=201)
async def create_sprint(request: Request, project: ProjectName, body: TasksSprintIn, user: CurrentUser):
    """A new sprint, planned or active (one sprint of a project is active at a time)."""

    async def work(conn):
        access, config = await _opened(conn, user, project)
        writer = _writer(access, user, None, config, "creating a sprint")
        await writes.create_sprint(conn, writer, writes.SprintInput(**body.model_dump()))
        return sprint_model(await store.sprint(conn, access.project_id, body.name))

    return await _answer(request, work)


@router.patch("/{project}/sprints/{name}", response_model=TasksSprint)
async def edit_sprint(
    request: Request, project: ProjectName, name: SprintPath, body: TasksSprintEdit, user: CurrentUser
):
    """Change a sprint; closing it moves its unfinished tasks to ``move_to``, or to no sprint."""

    async def work(conn):
        access, config = await _opened(conn, user, project)
        writer = _writer(access, user, None, config, "changing a sprint")
        now = await writes.edit_sprint(conn, writer, name, body.values(), body.move_to)
        return sprint_model(await store.sprint(conn, access.project_id, now))

    return await _answer(request, work)


@router.post("/{project}/import", response_model=TasksImportResult)
async def import_tasks(request: Request, project: ProjectName, body: TasksImport, user: CurrentUser, sink: Sink = None):
    """Import evo-tasks/1 JSON once: its sprints and tasks, linked to SRS items by code, to plan steps and pull
    requests; ``dry_run`` reads it against the project and imports nothing."""

    async def work(conn):
        access, config = await _opened(conn, user, project)
        writer = _writer(access, user, sink, config, "importing tasks")
        found = await imports.plan(conn, writer, body.data)
        made = [] if body.dry_run else await imports.apply(conn, writer, found)
        if made:
            await _graph_changed(request, conn, access)
        return import_model(config, found, body.dry_run, made)

    return await _answer(request, work)


__all__ = ["router"]
