"""The API of the hub for plugins, the task runs it dispatches, and their pull requests (``evo_agents.hub.task_runs``;
``docs/plugins.md``, section Agent types).

A route under /v1/plugin-api/ takes the token of a plugin's backend (``Authorization: Bearer evp_...``,
``plugin_tokens``) and no other credential: the hub's guard leaves the check to it (``security.PUBLIC_PREFIXES``).
No token, a malformed one, or one that is revoked or whose plugin is not installed or is turned off gets 401. It acts
in a project that enabled the plugin and granted it ``runs:dispatch``, 403 otherwise, and only on the plugin's own
agent types and tasks, 404 for any other.

- POST /v1/plugin-api/projects/{p}/agent-types/{name}/tasks: hand a task to agent type ``name`` of the plugin. 404
  for a type its installed version does not contribute, or that the project did not enable or turned off; 409 when the
  type's profile was deleted or its ``run_as`` no longer holds writer on the project; 403 when the primary seat of
  ``run_as`` in the org chart ended; 429 once what is left of its cap, after its runs of the last BUDGET_HOURS and
  what the runs not ended hold, is under the floor of a run (``task_runs.left_problem``). Otherwise a run of kind task, dispatched by ``run_as``
  with the credential kind machine, under the type's profile, on the branch ``task/<run>`` of each repo of the project,
  with what is left of the cap as its cost cap (``task_runs.run_budget``), queued for a worker of ``run_as`` the type
  names (``claims``). Audited as task.dispatch in the project, ``run_as`` its actor. A task that names ``repo`` and
  ``branch`` runs on that branch of that repo instead: 422 for a repo the project lacks or a branch a task may not
  name (``task_runs.branch_problem``: a default branch, or a branch of another kind of run), 409 while another task
  run of the project that is not over works on it. ``minutes`` asks for up to MAX_MINUTES of agent time. ``repos``
  names the repos of the project the run gets a worktree of (422 for one it lacks; default: every repo), each by its
  name or as ``{repo, base}`` (``TaskRepo``), and ``base`` a branch of origin each worktree starts from instead of the
  default branch (``task_runs.base_problem``, 422), which the draft pull request of each push goes into too; the base a
  repo of ``repos`` names is that repo's alone, in place of ``base``.
- GET /v1/plugin-api/projects/{p}/tasks/{task}: a task the plugin handed, by the id its dispatch answered: the latest
  attempt of its run, its state, branch and pull requests, ids and states only.

``hand_own_task`` is the same dispatch for a core plugin's Python, in the caller's transaction, with no token and no
``runs:dispatch`` grant: the image trusts the plugin, and the admin who enabled the type chose its run_as and its cap.

POST /v1/worker/runs/{id}/task/pulls: the worker that holds a task run pushed the run's branch of a repo; the hub opens
a draft pull request of it with the workers' App, into the base its task names for the repo or else the repo's default
branch, or finds the one open, never merges it, keeps it in runs.pulls and says so in the run's log. A repo elsewhere,
or a hub without the App, gets none.

``task_prompt`` is the prompt of a task run at its claim, and ``task_run_ended`` stores the event task.result for the
plugin of the run's agent type when the run ends (``run_state.record_move``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import MAX_ID, RunId
from evo_agents.hub.server.runs.models.task_dispatch import Task as Task
from evo_agents.hub.server.runs.models.task_dispatch import TaskBudget as TaskBudget
from evo_agents.hub.server.runs.models.task_dispatch import TaskIn as TaskIn
from evo_agents.hub.server.runs.models.task_dispatch import TaskPull as TaskPull
from evo_agents.hub.server.runs.models.task_dispatch import TaskPushed as TaskPushed
from evo_agents.hub.server.runs.models.task_dispatch import TaskQueued as TaskQueued
from evo_agents.hub.server.runs.models.task_dispatch import TaskRepo as TaskRepo
from evo_agents.hub.server.runs.service import task_dispatch as service
from evo_agents.hub.server.runs.service.task_dispatch import (  # noqa: F401 (the plugin API, core plugins, ops, run_state, run_specs and question_dispatch call them here)
    hand_again,
    hand_own_task,
    plugin_of_request,
    task_prompt,
    task_run_ended,
    task_select,
    task_view,
    type_refusal,
)
from evo_agents.hub.server.security import CurrentUser
from evo_agents.plugins.runtimes import CONTRIBUTION

TypeName = Annotated[str, Path(pattern=f"^{CONTRIBUTION}$", description="the agent type, as the manifest names it")]
TaskId = Annotated[int, Path(ge=1, le=MAX_ID, description="the id the task's dispatch answered")]
REFUSALS = {code: {"model": ErrorBody} for code in (401, 403, 404, 409, 422, 429)}

router = APIRouter(prefix="/v1/plugin-api", tags=["plugin api"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


@router.post(
    "/projects/{project}/agent-types/{name}/tasks", status_code=201, response_model=TaskQueued, responses=REFUSALS
)
async def dispatch_task(request: Request, project: ProjectName, name: TypeName, body: TaskIn) -> TaskQueued:
    """Hand a task to agent type ``name`` of the plugin whose token the request carries: a run of kind task."""
    authorization = request.headers.get("authorization")
    return await service.dispatch_task(request.app.state.engine, authorization, project, name, body)


@router.get("/projects/{project}/tasks/{task}", response_model=Task, responses=REFUSALS)
async def show_task(request: Request, project: ProjectName, task: TaskId) -> Task:
    """A task the plugin whose token the request carries handed, as its latest run has it."""
    return await service.show_task(request.app.state.engine, request.headers.get("authorization"), project, task)


@worker_router.post("/runs/{run_id}/task/pulls", status_code=201, response_model=TaskPull, responses=REFUSALS)
async def pushed(request: Request, run_id: RunId, body: TaskPushed, user: CurrentUser) -> TaskPull:
    """The worker pushed the task run's branch of a repo: its draft pull request, kept on the run."""
    return await service.pushed(request.app.state.engine, request.app.state.github_app, user, run_id, body)
