"""The tasks of a project an isolated plugin keeps with its token (``docs/plugins.md``, section Permissions), granted
``tasks:write`` in a project that has the feature tasks on (``plugin_api_access``):

- POST /v1/plugin-api/projects/{p}/board/tasks: a new task under ``key``, a key of the plugin's own, with its type,
  title, Markdown description, status, people and links (SRS items by code, runs, plan steps, pull requests); 409
  when a task of the project has that key already, which the answer names.
- GET .../board/tasks?key=K: the task the plugin made under key K, a list of one, or empty.
- PATCH .../board/tasks/{ref}: change a task the plugin made, as a member's PATCH changes one (its status, fields,
  people and links); 404 for a task of the project the plugin did not make.
- POST .../board/tasks/{ref}/comments: comment on a task the plugin made.

The prefix ``board`` keeps them apart from GET .../tasks/{task}, a task run the plugin handed (``task_dispatch``). Each
route runs the code of a member's request (``evo_agents.hub.server.tasks``) in the name of the admin who granted the
plugin its permissions (``plugin_api_access.acting``), who is the author of the task, of each change in its history and
of each comment. A task the plugin makes carries the project's label, and its external key is ``<plugin>:<key>``
(tasks_items.external_key, unique in the project), so a plugin finds and changes only the tasks it made, and never a
task an import or a member made; its ``external_ref`` names the plugin and the key. Creating or changing a task queues
a build of the project's knowledge graph, as a member's request does. A refusal is the hub's error body; nothing is
written.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.plugin_api_access import Acting, acting, feature_on, granted
from evo_agents.hub.server.task_dispatch import plugin_of_request
from evo_agents.hub.server.tasks import store, writes
from evo_agents.hub.server.tasks.models import (
    MAX_PEOPLE,
    TasksComment,
    TasksCommentIn,
    TasksItemDetail,
    TasksItemEdit,
    TasksLinksIn,
    TasksRolesIn,
    comment_model,
)
from evo_agents.hub.server.tasks.server import task_detail
from evo_agents.hub.server.tasks.settings import Settings, TasksError, number_of, settings
from evo_agents.hub.tables_tasks import MAX_DESCRIPTION_CHARS, MAX_TITLE_CHARS, PRIORITIES

log = logging.getLogger(__name__)

PERMISSION = "tasks:write"
FEATURE = "tasks"
MAX_KEY_CHARS = 200
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
RACE = "another write in the project took a number or a key at the same moment: nothing was written; try again"
Ref = Annotated[str, Path(min_length=1, max_length=20, description="the task's number, as T-42 or 42")]
Key = Annotated[str, Query(min_length=1, max_length=MAX_KEY_CHARS, description="the key the plugin made it under")]
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (401, 403, 404, 409, 422)}

router = APIRouter(prefix="/v1/plugin-api", tags=["plugin api"], responses={401: {"model": ErrorBody}})


class PluginTaskIn(BaseModel):
    """A new task of the plugin, under a key of its own; its type and status are the config's first when left out."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(
        min_length=1,
        max_length=MAX_KEY_CHARS,
        description="the plugin's own key of the task, unique among its tasks in the project: GET ?key= finds it",
    )
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    description_md: str = Field("", max_length=MAX_DESCRIPTION_CHARS, description="Markdown")
    type: str | None = Field(None, max_length=40, description="one of the config's types; default: its first")
    status: str | None = Field(None, max_length=40, description="a key of the config's statuses; default: its first")
    priority: str = Field("medium", max_length=20, description=f"one of {', '.join(PRIORITIES)}")
    assignees: list[str] = Field(default_factory=list, max_length=MAX_PEOPLE)
    roles: TasksRolesIn = Field(default_factory=lambda: TasksRolesIn.model_validate({}))
    links: TasksLinksIn = Field(default_factory=lambda: TasksLinksIn())

    @field_validator("key")
    @classmethod
    def _key(cls, key: str) -> str:
        if CONTROL.search(key):
            raise ValueError("the key has no control characters")
        return key


class PluginTask(TasksItemDetail):
    """A task the plugin made, whole, with the key it made it under."""

    key: str = Field(description="the plugin's own key of the task")


# Who acts, and on which task


async def _acting(request: Request, conn: AsyncConnection, project: str) -> tuple[Acting, Settings]:
    plugin_id = await plugin_of_request(request, conn)
    enabled = await granted(conn, plugin_id, project, PERMISSION)
    await feature_on(conn, FEATURE, enabled)
    who = await acting(conn, enabled)
    return who, await settings(conn, who.access.project_id, who.access.name)


def _stored(who: Acting, key: str) -> str:
    return f"{who.plugin}:{key}"


def _writer(who: Acting, config: Settings) -> writes.Writer:
    store.writer(who.access, f"plugin {who.plugin} writing a task")
    return writes.Writer(who.access, who.user_id, config, None)


async def _own(conn: AsyncConnection, who: Acting, ref: str) -> tuple[int, str]:
    """(number, key) of task ``ref`` when the plugin made it; 404 for any other task of the project, or none."""
    t = tables.tasks.items
    number = number_of(ref)
    query = select(t.c.external_key).where(t.c.project_id == who.access.project_id, t.c.number == number)
    stored = (await conn.execute(query)).scalar_one_or_none()
    prefix = _stored(who, "")
    if stored is None or not stored.startswith(prefix):
        raise TasksError(404, f"plugin {who.plugin} made no task {ref} in project {who.access.name}")
    return number, stored[len(prefix) :]


async def _shown(conn: AsyncConnection, who: Acting, config: Settings, number: int, key: str) -> PluginTask:
    found = await task_detail(conn, who.access, config, number, None)
    return PluginTask(**found.model_dump(), key=key)


async def _answer(request: Request, work):
    """``work(conn)`` in one transaction; a refusal of the feature tasks as the hub's error body, nothing written."""
    try:
        async with request.app.state.engine.begin() as conn:
            return await work(conn)
    except TasksError as exc:
        return error_response(request, exc.status, exc.message, detail=exc.detail)
    except IntegrityError:
        return error_response(request, 409, RACE)


async def _graph_changed(request: Request, conn: AsyncConnection, who: Acting) -> None:
    await queue_if_configured(request.app.state.jobs, conn, who.access.project_id, who.access.name)


# The routes


@router.post("/projects/{project}/board/tasks", status_code=201, response_model=PluginTask, responses=REFUSALS)
async def create_task(request: Request, project: ProjectName, body: PluginTaskIn):
    """A new task of the plugin under ``key``, in the name of the admin who granted the plugin its permissions."""

    async def work(conn: AsyncConnection):
        who, config = await _acting(request, conn, project)
        writer = _writer(who, config)
        stored = _stored(who, body.key)
        t = tables.tasks.items
        query = select(t.c.number).where(t.c.project_id == who.access.project_id, t.c.external_key == stored)
        held = (await conn.execute(query)).scalar_one_or_none()
        if held is not None:
            raise TasksError(409, f"plugin {who.plugin} made task {config.ref(held)} under key {body.key} already")
        task = writes.TaskInput(
            title=body.title,
            description_md=body.description_md,
            type=body.type,
            status=body.status,
            priority=body.priority,
            assignees=body.assignees,
            roles=body.roles.given(),
            links=writes.LinksIn(**body.links.model_dump()),
            external_key=stored,
            external_ref={"plugin": who.plugin, "key": body.key},
        )
        number = await writes.create(conn, writer, task)
        await _graph_changed(request, conn, who)
        log.info("plugin task created", extra={"project": project, "plugin": who.plugin, "task": config.ref(number)})
        return await _shown(conn, who, config, number, body.key)

    return await _answer(request, work)


@router.get("/projects/{project}/board/tasks", response_model=list[PluginTask], responses=REFUSALS)
async def find_tasks(request: Request, project: ProjectName, key: Key):
    """The task the plugin made under ``key``: a list of one, or empty."""

    async def work(conn: AsyncConnection):
        who, config = await _acting(request, conn, project)
        t = tables.tasks.items
        query = select(t.c.number).where(t.c.project_id == who.access.project_id, t.c.external_key == _stored(who, key))
        number = (await conn.execute(query)).scalar_one_or_none()
        return [] if number is None else [await _shown(conn, who, config, number, key)]

    return await _answer(request, work)


@router.patch("/projects/{project}/board/tasks/{ref}", response_model=PluginTask, responses=REFUSALS)
async def edit_task(request: Request, project: ProjectName, ref: Ref, body: TasksItemEdit):
    """Change a task the plugin made: its fields, its status, its people, its links."""

    async def work(conn: AsyncConnection):
        who, config = await _acting(request, conn, project)
        writer = _writer(who, config)
        number, key = await _own(conn, who, ref)
        row = await store.task(conn, who.access, number, None, lock=True)
        changes = writes.TaskChanges(
            values=body.values(),
            assignees=body.assignees,
            roles=body.roles.given(),
            links=writes.LinksIn(**body.links.model_dump()),
            unlink=body.unlink,
        )
        await writes.edit(conn, writer, row, changes)
        await _graph_changed(request, conn, who)
        return await _shown(conn, who, config, number, key)

    return await _answer(request, work)


@router.post(
    "/projects/{project}/board/tasks/{ref}/comments", status_code=201, response_model=TasksComment, responses=REFUSALS
)
async def add_comment(request: Request, project: ProjectName, ref: Ref, body: TasksCommentIn):
    """Comment on a task the plugin made."""

    async def work(conn: AsyncConnection):
        who, config = await _acting(request, conn, project)
        writer = _writer(who, config)
        number, _ = await _own(conn, who, ref)
        row = await store.task(conn, who.access, number, None)
        comment_id = await writes.comment(conn, writer, row.id, body.body)
        return comment_model(next(c for c in await store.comments(conn, row.id) if c.id == comment_id))

    return await _answer(request, work)


__all__ = ["router"]
