"""The routes of a member's task runs: each one hands the request's parts and the app's engine to the service."""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.member_tasks import service
from evo_agents.hub.server.member_tasks.models import REFUSALS, MemberTask, MemberTaskIn, TaskId
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["task runs"], responses={401: {"model": ErrorBody}})


@router.post("/{project}/task-runs", status_code=201, response_model=MemberTask, responses=REFUSALS)
async def dispatch_task_run(
    request: Request, project: ProjectName, body: MemberTaskIn, user: CurrentUser
) -> MemberTask:
    """Hand an agent a task with a JSON input and external assets, in no repo: a run of kind task under the caller,
    which needs the writer role on the project."""
    return await service.dispatch(request.app.state.engine, user, project, body)


@router.get("/{project}/task-runs/{task}", response_model=MemberTask, responses=REFUSALS)
async def show_task_run(request: Request, project: ProjectName, task: TaskId, user: CurrentUser) -> MemberTask:
    """A task a member handed, as its latest attempt has it: its state and its output, for whoever reads its run."""
    return await service.shown(request.app.state.engine, user, project, task)
