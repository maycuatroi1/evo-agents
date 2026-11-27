"""The routes of the plugin tasks, under /v1/x/tasks/<project>/ for a member with a grant on a project that enabled
it (the hub checks both first, ``evo_agents.hub.server.plugins.service.extension``; a project that did not enable it answers 404).

Readers read: the config as tasks are written against it, the tasks (by status, sprint, the caller's own or an
assignee's), a task whole with its links as they are now, its history and comments, the sprints, the members a task
may name. Writers create and change tasks, link and unlink them, set their people, block and unblock them, comment, keep sprints and import
``evo-tasks/1`` JSON once.
Reads go through the sink X-Evo-Sink names, else the project's hub sink. A refusal is the hub's error body, with an
import's problems in ``detail``. Creating, changing or importing tasks queues a build of the project's knowledge graph,
whose tickets they are. Each route runs what ``service.requests`` does in one transaction.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Path, Query, Request
from sqlalchemy.exc import IntegrityError

from evo_agents.hub.server.base.models import PROJECT_NAME
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.server.tasks.models.api import (
    TasksBlocker,
    TasksBlockerEdit,
    TasksBlockerIn,
    TasksComment,
    TasksCommentIn,
    TasksImport,
    TasksImportResult,
    TasksItem,
    TasksItemDetail,
    TasksItemEdit,
    TasksItemIn,
    TasksPerson,
    TasksSettings,
    TasksSprint,
    TasksSprintEdit,
    TasksSprintIn,
)
from evo_agents.hub.server.tasks.models.values import MAX_LISTED, SINK_HEADER, Filters, TasksError
from evo_agents.hub.server.tasks.service import requests

REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 413, 422)}
RACE = "another write in the project took a number or a name at the same moment: nothing was written; try again"
ProjectName = Annotated[str, Path(pattern=PROJECT_NAME)]
Ref = Annotated[str, Path(min_length=1, max_length=20, description="the task's number, as T-42 or 42")]
SprintPath = Annotated[str, Path(min_length=1, max_length=100)]
Sink = Annotated[str | None, Header(alias=SINK_HEADER)]

router = APIRouter(tags=["plugins"], responses={401: {"model": ErrorBody}, **REFUSALS})


async def _answer(request: Request, work):
    """``work(conn)`` in one transaction; a refusal as the hub's error body, nothing written."""
    try:
        async with UnitOfWork(request.app.state.engine) as conn:
            return await work(conn)
    except TasksError as exc:
        return error_response(request, exc.status, exc.message, detail=exc.detail)
    except IntegrityError:
        return error_response(request, 409, RACE)


def _jobs(request: Request):
    return request.app.state.jobs


# Reading


@router.get("/{project}/settings", response_model=TasksSettings)
async def show_settings(request: Request, project: ProjectName, user: CurrentUser):
    """The plugin's config in the project: the prefix, the statuses with their groups, the types."""
    return await _answer(request, lambda conn: requests.show_settings(conn, user, project))


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
    blocked: Annotated[bool, Query(description="the tasks that are blocked")] = False,
    limit: Annotated[int, Query(ge=1, le=MAX_LISTED)] = 500,
    sink: Sink = None,
):
    """The tasks of the project the caller sees, by number, that the filters keep."""
    member = user.user_id if mine else None
    filters = Filters(status, sprint, backlog, member, assignee, limit, blocked)
    return await _answer(request, lambda conn: requests.list_tasks(conn, user, project, filters, sink))


@router.get("/{project}/tasks/{ref}", response_model=TasksItemDetail)
async def show_task(request: Request, project: ProjectName, ref: Ref, user: CurrentUser, sink: Sink = None):
    """A task whole: its links as they are now (a plan step's status read from the plan), history and comments."""
    return await _answer(request, lambda conn: requests.show_task(conn, user, project, ref, sink))


@router.get("/{project}/sprints", response_model=list[TasksSprint])
async def list_sprints(request: Request, project: ProjectName, user: CurrentUser):
    """The sprints of the project, the newest first."""
    return await _answer(request, lambda conn: requests.list_sprints(conn, user, project))


@router.get("/{project}/people", response_model=list[TasksPerson])
async def list_people(request: Request, project: ProjectName, user: CurrentUser):
    """The members of the project, whom a task may name as an assignee, a BA, a QA or a Dev, by login."""
    return await _answer(request, lambda conn: requests.list_people(conn, user, project))


# Writing


@router.post("/{project}/tasks", response_model=TasksItemDetail, status_code=201)
async def create_task(request: Request, project: ProjectName, body: TasksItemIn, user: CurrentUser, sink: Sink = None):
    """A new task, with its people and links."""
    jobs = _jobs(request)
    return await _answer(request, lambda conn: requests.create_task(conn, jobs, user, project, body, sink))


@router.patch("/{project}/tasks/{ref}", response_model=TasksItemDetail)
async def edit_task(
    request: Request, project: ProjectName, ref: Ref, body: TasksItemEdit, user: CurrentUser, sink: Sink = None
):
    """Change a task: its fields, its people, its links."""
    jobs = _jobs(request)
    return await _answer(request, lambda conn: requests.edit_task(conn, jobs, user, project, ref, body, sink))


@router.post("/{project}/tasks/{ref}/comments", response_model=TasksComment, status_code=201)
async def add_comment(
    request: Request, project: ProjectName, ref: Ref, body: TasksCommentIn, user: CurrentUser, sink: Sink = None
):
    """Comment on a task."""
    return await _answer(request, lambda conn: requests.add_comment(conn, user, project, ref, body, sink))


@router.post("/{project}/tasks/{ref}/blockers", response_model=TasksBlocker, status_code=201)
async def add_blocker(
    request: Request, project: ProjectName, ref: Ref, body: TasksBlockerIn, user: CurrentUser, sink: Sink = None
):
    """Block a task: why, and what it waits on (a member, another task of the project, or a text)."""
    return await _answer(request, lambda conn: requests.add_blocker(conn, user, project, ref, body, sink))


@router.patch("/{project}/tasks/{ref}/blockers/{blocker_id}", response_model=TasksBlocker)
async def edit_blocker(
    request: Request,
    project: ProjectName,
    ref: Ref,
    blocker_id: Annotated[int, Path(ge=1)],
    body: TasksBlockerEdit,
    user: CurrentUser,
    sink: Sink = None,
):
    """Change a blocker set by hand: its reason, what it waits on, or clear it (``cleared``)."""
    return await _answer(request, lambda conn: requests.edit_blocker(conn, user, project, ref, blocker_id, body, sink))


@router.post("/{project}/sprints", response_model=TasksSprint, status_code=201)
async def create_sprint(request: Request, project: ProjectName, body: TasksSprintIn, user: CurrentUser):
    """A new sprint, planned or active (one sprint of a project is active at a time)."""
    return await _answer(request, lambda conn: requests.create_sprint(conn, user, project, body))


@router.patch("/{project}/sprints/{name}", response_model=TasksSprint)
async def edit_sprint(
    request: Request, project: ProjectName, name: SprintPath, body: TasksSprintEdit, user: CurrentUser
):
    """Change a sprint; closing it moves its unfinished tasks to ``move_to``, or to no sprint."""
    return await _answer(request, lambda conn: requests.edit_sprint(conn, user, project, name, body))


@router.post("/{project}/import", response_model=TasksImportResult)
async def import_tasks(request: Request, project: ProjectName, body: TasksImport, user: CurrentUser, sink: Sink = None):
    """Import evo-tasks/1 JSON once: its sprints and tasks, linked to SRS items by code, to plan steps and pull
    requests; ``dry_run`` reads it against the project and imports nothing."""
    jobs = _jobs(request)
    return await _answer(request, lambda conn: requests.import_tasks(conn, jobs, user, project, body, sink))


__all__ = ["router"]
