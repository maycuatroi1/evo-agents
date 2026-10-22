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
  ``run_as`` in the org chart ended; 429 once its task runs cost its cap in the last BUDGET_HOURS
  (``task_runs.spent_problem``). Otherwise a run of kind task, dispatched by ``run_as``
  with the credential kind machine, under the type's profile, on the branch ``task/<run>`` of each repo of the project,
  with what is left of the cap as its cost cap (``task_runs.run_budget``), queued for a worker of ``run_as`` the type
  names (``claims``). Audited as task.dispatch in the project, ``run_as`` its actor.
- GET /v1/plugin-api/projects/{p}/tasks/{task}: a task the plugin handed, by the id its dispatch answered: the latest
  attempt of its run, its state, branch and pull requests, ids and states only.

``hand_own_task`` is the same dispatch for a core plugin's Python, in the caller's transaction, with no token and no
``runs:dispatch`` grant: the image trusts the plugin, and the admin who enabled the type chose its run_as and its cap.

POST /v1/worker/runs/{id}/task/pulls: the worker that holds a task run pushed the run's branch of a repo; the hub opens
a draft pull request of it with the workers' App, or finds the one open, never merges it, keeps it in runs.pulls and
says so in the run's log. A repo elsewhere, or a hub without the App, gets none.

``task_prompt`` is the prompt of a task run at its claim, and ``task_run_ended`` stores the event task.result for the
plugin of the run's agent type when the run ends (``run_state.record_move``).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import null, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import plugin_calls, plugin_events, profiles, runs, tables, task_runs
from evo_agents.hub.access import has_role
from evo_agents.hub.credentials import github_repo
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.agent_types import contributed, enabled_type, type_spend
from evo_agents.hub.server.chat_agents import open_draft
from evo_agents.hub.server.chat_runs import project_repos
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.org_store import ended_refusal
from evo_agents.hub.server.plugin_tokens import plugin_of_token
from evo_agents.hub.server.profile_store import by_id
from evo_agents.hub.server.run_state import notify_queued, write_event
from evo_agents.hub.server.runs.models import MAX_ID, NOT_HELD, OBJECT_NAME, RunId
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.runs.service.dispatch import insert_run
from evo_agents.hub.server.security import CurrentUser
from evo_agents.plugins.runtimes import CONTRIBUTION

log = logging.getLogger(__name__)

TypeName = Annotated[str, Path(pattern=f"^{CONTRIBUTION}$", description="the agent type, as the manifest names it")]
TaskId = Annotated[int, Path(ge=1, le=MAX_ID, description="the id the task's dispatch answered")]
TOKEN_HINT = "a live plugin token is required: Authorization: Bearer evp_..., the token the plugin host hands a backend"
REFUSALS = {code: {"model": ErrorBody} for code in (401, 403, 404, 409, 422, 429)}

router = APIRouter(prefix="/v1/plugin-api", tags=["plugin api"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


class TaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(
        min_length=1,
        max_length=task_runs.MAX_TASK_BYTES,
        description=f"what the agent is to do, at most {task_runs.MAX_TASK_BYTES} bytes in UTF-8",
    )
    title: str | None = Field(
        None, max_length=task_runs.MAX_TITLE_CHARS, description="one line the run's list shows; default: the task's"
    )


class TaskBudget(BaseModel):
    max_usd: float = Field(description="what is left of the agent type's cap, the run's cost cap")
    max_seconds: int


class Task(BaseModel):
    """A task a plugin handed one of its agent types, as its latest run has it: ids and states only."""

    task_id: int = Field(description="the run its dispatch made, which names its branch")
    run_id: int = Field(description="its latest run: a run its worker lost goes on in the next attempt")
    attempt: int
    project: str
    agent_type: str
    state: str
    branch: str
    pulls: list[dict] = Field(description="the draft pull requests the hub opened, {repo, url, number} each")
    failure_cause: str | None
    queued_at: datetime
    finished_at: datetime | None


class TaskQueued(Task):
    budget: TaskBudget
    spent_usd: float = Field(description=f"what its agent type's task runs cost in the last {task_runs.BUDGET_HOURS} h")
    daily_usd: float = Field(description="the agent type's cap, which the project's admins set")


class TaskPushed(BaseModel):
    repo: str = Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")
    branch: str = Field(min_length=1, max_length=255, description="the run's branch, task/<id>")
    head: str = Field(pattern=OBJECT_NAME, description="the commit the branch has now")


class TaskPull(BaseModel):
    repo: str
    branch: str
    url: str | None = Field(description="the draft pull request, or the branch; null when the hub knows neither")
    number: int | None = Field(description="the pull request's number on GitHub")
    opened: bool = Field(description="the hub opened it now; false for one open already, or none")
    problem: str | None = Field(description="why GitHub opened none, when it refused")


# The plugin and its task runs


async def plugin_of_request(request: Request, conn: AsyncConnection) -> int:
    """The plugin whose live token the request carries; 401 for none."""
    scheme, _, token = (request.headers.get("authorization") or "").strip().partition(" ")
    plugin_id = None
    if scheme.lower() == "bearer" and token.strip():
        plugin_id = await plugin_of_token(conn, token.strip())
    if plugin_id is None:
        raise HTTPException(401, TOKEN_HINT, headers={"WWW-Authenticate": "Bearer"})
    return plugin_id


async def _dispatcher(conn: AsyncConnection, plugin_id: int, project: str):
    """The row of ``plugin_calls.enabled_query`` of plugin ``plugin_id`` in ``project``, granted runs:dispatch; 403
    otherwise, the same for a project that does not exist."""
    p = tables.projects
    query = plugin_calls.enabled_query().where(tables.plugins.c.id == plugin_id, p.c.name == project)
    enabled = (await conn.execute(query)).first()
    if enabled is None or task_runs.DISPATCH_PERMISSION not in plugin_calls.effective_grant(enabled):
        raise HTTPException(
            403,
            f"the plugin is not enabled in project {project} with {task_runs.DISPATCH_PERMISSION} granted: an admin of "
            "the project enables it and grants what it asks for",
        )
    return enabled


def _task_select():
    """A task run with its project's and agent type's names."""
    r, t, p = tables.runs, tables.project_agent_types, tables.projects
    return (
        select(
            r.c.id,
            r.c.attempt,
            p.c.name.label("project"),
            r.c.project_id,
            t.c.name.label("agent_type"),
            t.c.plugin_id,
            r.c.state,
            r.c.branch,
            r.c.pulls,
            r.c.failure_cause,
            r.c.queued_at,
            r.c.finished_at,
        )
        .join_from(r, t, t.c.id == r.c.agent_type_id)
        .join(p, p.c.id == r.c.project_id)
    )


def _task(row) -> dict:
    return {
        "task_id": task_runs.task_of(row.branch) or row.id,
        "run_id": row.id,
        "attempt": row.attempt,
        "project": row.project,
        "agent_type": row.agent_type,
        "state": row.state,
        "branch": row.branch,
        "pulls": list(row.pulls or []),
        "failure_cause": row.failure_cause,
        "queued_at": row.queued_at,
        "finished_at": row.finished_at,
    }


async def _type_refusal(conn: AsyncConnection, enabled, row, name: str) -> tuple[int, str] | None:
    """(status, why) agent type ``name`` (``row`` of ``agent_types.enabled_type``) takes no task now, or None."""
    if contributed(enabled.manifest, name) is None or row is None or row.disabled_at is not None:
        return 404, (
            f"agent type {name} of plugin {enabled.name} is not enabled in project {enabled.project}, or it was turned "
            f"off, or version {enabled.installed_version} contributes none: an admin of the project enables it"
        )
    if row.profile_deleted_at is not None:
        return 409, f"@{row.handle}, the profile of agent type {name}, was deleted: it takes no new work"
    g = tables.grants
    query = select(g.c.role).where(g.c.user_id == row.run_as_id, g.c.project_id == enabled.project_id)
    role = (await conn.execute(query)).scalar_one_or_none()
    if not has_role(role, "writer"):
        return 409, (
            f"agent type {name} runs as {row.run_as}, who holds {role or 'no grant'} on project {enabled.project} now, "
            "not writer: an admin of the project names another member"
        )
    ended = await ended_refusal(conn, row.run_as_id, row.run_as, what=f"a task of agent type {name}")
    return None if ended is None else (403, ended)


def _title(body: TaskIn, name: str) -> str:
    return runs.step_title({"title": body.title or body.task}) or f"task of agent type {name}"


async def _queue(conn: AsyncConnection, enabled, row, body: TaskIn, left: float) -> int:
    """Queue the task run of ``body`` for agent type ``row`` on the branch task/<its id> of each repo of the project."""
    profile = await by_id(conn, row.profile_id)
    allow = profiles.allowed(profile.permissions, "model")
    values = {
        "kind": "task",
        "project_id": enabled.project_id,
        "title": _title(body, row.name),
        "dispatched_by": row.run_as_id,
        "dispatched_via": "machine",
        "requested_runtime": "any",
        "runtime": "any",
        "model": allow[0] if allow else None,
        "mode": "headless",
        "approval": "auto",
        "timeout_s": task_runs.TIMEOUT_MIN * 60,
        "profile_id": profile.id,
        "profile_version": profile.version,
        "budget": task_runs.run_budget(left),
        "request": body.task,
        "agent_type_id": row.id,
    }
    run_id = await insert_run(conn, values)
    branch = task_runs.branch_of(run_id)
    names = await project_repos(conn, enabled.project_id)
    repos = [{"repo": name, "branch": branch} for name in names] or null()  # SQL NULL, not a JSON null
    r = tables.runs
    await conn.execute(update(r).values(branch=branch, repos=repos, pulls=[]).where(r.c.id == run_id))
    await notify_queued(conn, run_id)
    return run_id


async def _audited(conn: AsyncConnection, enabled, row, run_id: int) -> None:
    """Audit the dispatch of task run ``run_id`` of agent type ``row`` in the project of ``enabled``, by its run_as."""
    target = f"{enabled.project}/plugin:{enabled.name} agent-type:{row.name} run:{run_id}"
    await audit.record(
        conn,
        actor_id=row.run_as_id,
        token_id=None,
        action=audit.TASK_DISPATCH,
        target=target,
        project_id=enabled.project_id,
    )


async def hand_own_task(
    conn: AsyncConnection, plugin: str, project_id: int, name: str, task: str, title: str | None = None
) -> tuple[int | None, str | None]:
    """A core plugin hands ``task`` to its own agent type ``name`` in project ``project_id``, in the caller's
    transaction: (the task run, None), or (None, why it was not dispatched) for each refusal of the API's route but
    the token's and the grant's. ``task`` is cut to MAX_TASK_BYTES."""
    enabled = await plugin_calls.enabled_in(conn, plugin, project_id)
    if enabled is None:
        return None, f"plugin {plugin} is not enabled in the project, or it is turned off"
    row = await enabled_type(conn, project_id, enabled.id, name, lock=True)  # one dispatch of a type at once
    refusal = await _type_refusal(conn, enabled, row, name)
    if refusal is not None:
        return None, refusal[1]
    spent = await type_spend(conn, row.id)
    problem = task_runs.spent_problem(spent, row.daily_usd, name)
    if problem is not None:
        return None, problem
    body = TaskIn(
        task=runs.clip(task, task_runs.MAX_TASK_BYTES), title=(title or "")[: task_runs.MAX_TITLE_CHARS] or None
    )
    run_id = await _queue(conn, enabled, row, body, row.daily_usd - spent)
    await _audited(conn, enabled, row, run_id)
    log.info(
        "task dispatched", extra={"project_id": project_id, "plugin": plugin, "agent_type": name, "run_id": run_id}
    )
    return run_id, None


@router.post(
    "/projects/{project}/agent-types/{name}/tasks", status_code=201, response_model=TaskQueued, responses=REFUSALS
)
async def dispatch_task(request: Request, project: ProjectName, name: TypeName, body: TaskIn) -> TaskQueued:
    """Hand a task to agent type ``name`` of the plugin whose token the request carries: a run of kind task."""
    if len(body.task.encode()) > task_runs.MAX_TASK_BYTES:
        raise HTTPException(422, f"the task is longer than {task_runs.MAX_TASK_BYTES} bytes in UTF-8")
    async with request.app.state.engine.begin() as conn:
        plugin_id = await plugin_of_request(request, conn)
        enabled = await _dispatcher(conn, plugin_id, project)
        row = await enabled_type(conn, enabled.project_id, plugin_id, name, lock=True)  # one dispatch of a type at once
        refusal = await _type_refusal(conn, enabled, row, name)
        if refusal is not None:
            raise HTTPException(*refusal)
        spent = await type_spend(conn, row.id)
        problem = task_runs.spent_problem(spent, row.daily_usd, name)
        if problem is not None:
            raise HTTPException(429, problem)
        run_id = await _queue(conn, enabled, row, body, row.daily_usd - spent)
        await _audited(conn, enabled, row, run_id)
        found = (await conn.execute(_task_select().where(tables.runs.c.id == run_id))).one()
        budget = task_runs.run_budget(row.daily_usd - spent)
    log.info(
        "task dispatched", extra={"project": project, "plugin": enabled.name, "agent_type": name, "run_id": run_id}
    )
    return TaskQueued(**_task(found), budget=TaskBudget(**budget), spent_usd=spent, daily_usd=row.daily_usd)


@router.get("/projects/{project}/tasks/{task}", response_model=Task, responses=REFUSALS)
async def show_task(request: Request, project: ProjectName, task: TaskId) -> Task:
    """A task the plugin whose token the request carries handed, as its latest run has it."""
    r = tables.runs
    async with request.app.state.engine.begin() as conn:
        plugin_id = await plugin_of_request(request, conn)
        enabled = await _dispatcher(conn, plugin_id, project)
        query = _task_select().where(
            r.c.project_id == enabled.project_id,
            tables.project_agent_types.c.plugin_id == plugin_id,
            r.c.branch == task_runs.branch_of(task),
        )
        found = (await conn.execute(query.order_by(r.c.attempt.desc()).limit(1))).one_or_none()
    if found is None:
        raise HTTPException(404, f"the plugin handed no task {task} in project {project}")
    return Task(**_task(found))


# At the claim and at the end


async def task_prompt(conn: AsyncConnection, view) -> str:
    """The prompt of task run ``view`` (``runs.service.views.run_view``) as its worker claims it."""
    r, t, pl = tables.runs, tables.project_agent_types, tables.plugins
    query = (
        select(r.c.profile_id, t.c.name, pl.c.name.label("plugin"))
        .join_from(r, t, t.c.id == r.c.agent_type_id)
        .join(pl, pl.c.id == t.c.plugin_id)
        .where(r.c.id == view.id)
    )
    found = (await conn.execute(query)).one()
    profile = await by_id(conn, found.profile_id)
    return task_runs.build_task_prompt(
        agent={
            "handle": profile.handle,
            "name": profile.name,
            "project": profile.project,
            "description": profile.description,
        },
        plugin=found.plugin,
        agent_type=found.name,
        login=view.dispatched_by,
        branch=view.branch or task_runs.branch_of(view.id),
        task=view.request or "",
        repos=[repo.model_dump() for repo in view.repos or []],
    )


async def task_run_ended(conn: AsyncConnection, run_id: int, new: str) -> int:
    """Store the event task.result of task run ``run_id``, moved to ``new``, for the plugin of its agent type, when
    ``new`` ends the task (``task_runs.RESULT_STATES``); how many deliveries."""
    if new not in task_runs.RESULT_STATES:
        return 0
    found = (await conn.execute(_task_select().where(tables.runs.c.id == run_id))).one()
    payload = {key: value for key, value in _task(found).items() if key not in ("queued_at", "finished_at")}
    payload["state"] = new
    return await plugin_events.emit_task_result(conn, found.project_id, found.plugin_id, payload)


# The pull request of a task run's branch


async def _pushed_run(conn: AsyncConnection, user, run_id: int, body: TaskPushed):
    """(the run's title, the repo's origin) of a push of the task run ``run_id`` the worker reports; 404 or 422 when
    it is not the run's."""
    worker_id = (await worker_of(conn, user))[0]
    r = tables.runs
    query = select(r.c.worker_id, r.c.state, r.c.kind, r.c.project_id, r.c.branch, r.c.repos, r.c.title)
    row = (await conn.execute(query.where(r.c.id == run_id))).one_or_none()
    if row is None or row.worker_id != worker_id or row.state not in runs.HELD_STATES:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    if row.kind != "task":
        raise HTTPException(404, f"run {run_id} is a {row.kind} run: only a task run's branch gets a pull request here")
    if body.branch != row.branch or not task_runs.is_task_branch(body.branch):
        raise HTTPException(422, f"run {run_id} pushes {row.branch}, not {body.branch}")
    if body.repo not in [entry.get("repo") for entry in row.repos or [] if isinstance(entry, dict)]:
        raise HTTPException(422, f"run {run_id} does not work in {body.repo}")
    pr = tables.project_repos
    query = select(pr.c.origin).where(pr.c.project_id == row.project_id, pr.c.name == body.repo)
    return row.title, (await conn.execute(query)).scalar_one_or_none()


async def _keep(conn: AsyncConnection, run_id: int, pull: TaskPull) -> None:
    """Keep ``pull`` in runs.pulls of run ``run_id`` (one for each repo) when GitHub opened it, and say what happened
    in the run's log."""
    r = tables.runs
    pulls = (await conn.execute(select(r.c.pulls).where(r.c.id == run_id).with_for_update())).scalar_one() or []
    kept = {"repo": pull.repo, "url": pull.url, "number": pull.number}
    said = f"the hub opened the draft pull request {pull.url} of {pull.branch} of {pull.repo}; it is never merged"
    if pull.number is None:
        said = f"the hub opened no pull request of {pull.branch} of {pull.repo}: {pull.problem or 'no GitHub App'}"
    elif not pull.opened:
        said = f"the draft pull request {pull.url} of {pull.branch} of {pull.repo} was open already"
    if pull.number is not None:
        pulls = [item for item in pulls if item.get("repo") != pull.repo] + [kept]
        await conn.execute(update(r).where(r.c.id == run_id).values(pulls=pulls[-task_runs.MAX_PULLS :]))
    await write_event(conn, run_id, {"text": said, "pull": kept})


@worker_router.post("/runs/{run_id}/task/pulls", status_code=201, response_model=TaskPull, responses=REFUSALS)
async def pushed(request: Request, run_id: RunId, body: TaskPushed, user: CurrentUser) -> TaskPull:
    """The worker pushed the task run's branch of a repo: its draft pull request, kept on the run."""
    async with request.app.state.engine.begin() as conn:
        title, origin = await _pushed_run(conn, user, run_id, body)
        t, pl, r = tables.project_agent_types, tables.plugins, tables.runs
        query = select(t.c.name, pl.c.name).join_from(r, t, t.c.id == r.c.agent_type_id)
        agent_type, plugin = (
            await conn.execute(query.join(pl, pl.c.id == t.c.plugin_id).where(r.c.id == run_id))
        ).one()
    target = github_repo(origin) if origin else None
    app = request.app.state.github_app
    url, number, opened, problem = None, None, False, None
    if target is not None and app is not None:
        text = task_runs.DRAFT_TEXT.format(agent_type=agent_type, plugin=plugin, run=run_id)
        try:
            made, opened = await open_draft(app, target, body.branch, (title or body.branch)[:240], text)
            url, number = made.get("html_url"), made.get("number")
        except (GitHubRefused, GitHubUnavailable) as exc:
            problem = str(exc)
            log.warning("task run pull request not opened", extra={"run_id": run_id, "repo": body.repo, "why": problem})
    elif target is not None:
        url = f"https://github.com/{target[0]}/{target[1]}/tree/{body.branch}"
    pull = TaskPull(repo=body.repo, branch=body.branch, url=url, number=number, opened=opened, problem=problem)
    async with request.app.state.engine.begin() as conn:
        await _keep(conn, run_id, pull)
    log.info("task run branch pushed", extra={"run_id": run_id, "repo": body.repo, "pull": number, "opened": opened})
    return pull
