"""The tasks of a project an isolated plugin keeps with its token (``docs/plugins.md``, section Permissions), granted
``tasks:write`` in a project that has the feature tasks on (``service.tasks``):

- POST /v1/plugin-api/projects/{p}/board/tasks: a new task under ``key``, a key of the plugin's own, with its type,
  title, Markdown description, status, people and links (SRS items by code, runs, plan steps, pull requests); 409
  when a task of the project has that key already, which the answer names.
- GET .../board/tasks?key=K: the task the plugin made under key K, a list of one, or empty.
- PATCH .../board/tasks/{ref}: change a task the plugin made, as a member's PATCH changes one (its status, fields,
  people and links); 404 for a task of the project the plugin did not make.
- POST .../board/tasks/{ref}/comments: comment on a task the plugin made.

The prefix ``board`` keeps them apart from GET .../tasks/{task}, a task run the plugin handed (``task_dispatch``). A
refusal is the hub's error body; nothing is written.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.plugin_api.models.tasks import MAX_KEY_CHARS, RACE, PluginTask, PluginTaskIn
from evo_agents.hub.server.plugin_api.service import tasks
from evo_agents.hub.server.task_dispatch import plugin_of_request
from evo_agents.hub.server.tasks.models.api import TasksComment, TasksCommentIn, TasksItemEdit
from evo_agents.hub.server.tasks.models.values import TasksError

Ref = Annotated[str, Path(min_length=1, max_length=20, description="the task's number, as T-42 or 42")]
Key = Annotated[str, Query(min_length=1, max_length=MAX_KEY_CHARS, description="the key the plugin made it under")]
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (401, 403, 404, 409, 422)}

router = APIRouter(prefix="/v1/plugin-api", tags=["plugin api"], responses={401: {"model": ErrorBody}})


async def _answer(request: Request, work):
    """``work(conn, plugin_id)`` in one transaction; a refusal of the feature tasks as the hub's error body, nothing
    written."""
    try:
        async with request.app.state.engine.begin() as conn:
            return await work(conn, await plugin_of_request(request, conn))
    except TasksError as exc:
        return error_response(request, exc.status, exc.message, detail=exc.detail)
    except IntegrityError:
        return error_response(request, 409, RACE)


@router.post("/projects/{project}/board/tasks", status_code=201, response_model=PluginTask, responses=REFUSALS)
async def create_task(request: Request, project: ProjectName, body: PluginTaskIn):
    """A new task of the plugin under ``key``, in the name of the admin who granted the plugin its permissions."""

    async def work(conn: AsyncConnection, plugin_id: int):
        return await tasks.create(conn, request.app.state.jobs, plugin_id, project, body)

    return await _answer(request, work)


@router.get("/projects/{project}/board/tasks", response_model=list[PluginTask], responses=REFUSALS)
async def find_tasks(request: Request, project: ProjectName, key: Key):
    """The task the plugin made under ``key``: a list of one, or empty."""

    async def work(conn: AsyncConnection, plugin_id: int):
        return await tasks.find(conn, plugin_id, project, key)

    return await _answer(request, work)


@router.patch("/projects/{project}/board/tasks/{ref}", response_model=PluginTask, responses=REFUSALS)
async def edit_task(request: Request, project: ProjectName, ref: Ref, body: TasksItemEdit):
    """Change a task the plugin made: its fields, its status, its people, its links."""

    async def work(conn: AsyncConnection, plugin_id: int):
        return await tasks.edit(conn, request.app.state.jobs, plugin_id, project, ref, body)

    return await _answer(request, work)


@router.post(
    "/projects/{project}/board/tasks/{ref}/comments", status_code=201, response_model=TasksComment, responses=REFUSALS
)
async def add_comment(request: Request, project: ProjectName, ref: Ref, body: TasksCommentIn):
    """Comment on a task the plugin made."""

    async def work(conn: AsyncConnection, plugin_id: int):
        return await tasks.comment(conn, plugin_id, project, ref, body)

    return await _answer(request, work)


__all__ = ["router"]
