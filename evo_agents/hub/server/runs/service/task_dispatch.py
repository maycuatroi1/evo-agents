"""The task runs of plugins (``evo_agents.hub.server.task_dispatch``): the plugin a token names, the checks of a
dispatch, queueing a task run and its next attempt, a task as its latest run has it, its prompt at the claim, its
result at the end, and the draft pull request of a branch it pushed."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import guard, plugin_calls, plugin_events, profiles, runs, task_runs, webhook_events
from evo_agents.hub.credentials import github_repo
from evo_agents.hub.server import audit, run_access_rules
from evo_agents.hub.server.agents.service.agent_types import contributed, enabled_type, type_left, type_spend
from evo_agents.hub.server.autonomy.service import runs as run_autonomy
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.chats.repository.runs import project_repos
from evo_agents.hub.server.forge.models.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.merge.repository import draft_pulls as draft_rows
from evo_agents.hub.server.merge.service import draft_pulls, trailers
from evo_agents.hub.server.org_store import ended_refusal
from evo_agents.hub.server.plugins.service.tokens import plugin_of_token
from evo_agents.hub.server.profile_store import by_id
from evo_agents.hub.server.run_state import notify_queued, write_event
from evo_agents.hub.server.runs.models import NOT_HELD
from evo_agents.hub.server.runs.models.task_dispatch import (
    PULL_FIELDS,
    Task,
    TaskBudget,
    TaskIn,
    TaskPull,
    TaskPushed,
    TaskQueued,
)
from evo_agents.hub.server.runs.repository import task_dispatch as repository
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.runs.service.dispatch import insert_run
from evo_agents.hub.server.security import holds

log = logging.getLogger("evo_agents.hub.server.task_dispatch")

TOKEN_HINT = "a live plugin token is required: Authorization: Bearer evp_..., the token the plugin host hands a backend"


# The plugin and its task runs


async def plugin_of_authorization(conn: AsyncConnection, authorization: str | None) -> int:
    """The plugin whose live token the Authorization header ``authorization`` carries; 401 for none."""
    scheme, _, token = (authorization or "").strip().partition(" ")
    plugin_id = None
    if scheme.lower() == "bearer" and token.strip():
        plugin_id = await plugin_of_token(conn, token.strip())
    if plugin_id is None:
        raise HTTPException(401, TOKEN_HINT, headers={"WWW-Authenticate": "Bearer"})
    return plugin_id


async def plugin_of_request(request: Request, conn: AsyncConnection) -> int:
    """The plugin whose live token the request carries; 401 for none."""
    return await plugin_of_authorization(conn, request.headers.get("authorization"))


def dispatch_refusal(enabled, project: str) -> str | None:
    """Why a plugin whose row of ``plugin_calls.enabled_query`` is ``enabled`` may not dispatch in ``project``, or
    None."""
    if enabled is not None and task_runs.DISPATCH_PERMISSION in plugin_calls.effective_grant(enabled):
        return None
    return (
        f"the plugin is not enabled in project {project} with {task_runs.DISPATCH_PERMISSION} granted: an admin of "
        "the project enables it and grants what it asks for"
    )


async def dispatcher(conn: AsyncConnection, plugin_id: int, project: str):
    """The row of ``plugin_calls.enabled_query`` of plugin ``plugin_id`` in ``project``, granted runs:dispatch; 403
    otherwise, the same for a project that does not exist."""
    enabled = await repository.enabled_in_named(conn, plugin_id, project)
    refusal = dispatch_refusal(enabled, project)
    if refusal is not None:
        raise HTTPException(403, refusal)
    return enabled


task_select = repository.task_select  # a task run with its names: plugin_api runs


def task_view(row) -> dict:
    """The fields of ``Task`` of a row of ``task_select``."""
    return {
        "task_id": task_runs.task_of(row.branch) or row.id,
        "run_id": row.id,
        "attempt": row.attempt,
        "project": row.project,
        "agent_type": row.agent_type,
        "state": row.state,
        "branch": row.branch,
        # the plugin's contract: {repo, url, number} each (docs/plugins.md); the rest is the hub's (draft_pulls)
        "pulls": [{key: item.get(key) for key in PULL_FIELDS} for item in row.pulls or [] if isinstance(item, dict)],
        "failure_cause": row.failure_cause,
        "queued_at": row.queued_at,
        "finished_at": row.finished_at,
    }


async def type_refusal(conn: AsyncConnection, enabled, row, name: str) -> tuple[int, str] | None:
    """(status, why) agent type ``name`` (``row`` of ``agent_types.enabled_type``) takes no task now, or None."""
    if contributed(enabled.manifest, name) is None or row is None or row.disabled_at is not None:
        return 404, (
            f"agent type {name} of plugin {enabled.name} is not enabled in project {enabled.project}, or it was turned "
            f"off, or version {enabled.installed_version} contributes none: an admin of the project enables it"
        )
    if row.profile_deleted_at is not None:
        return 409, f"@{row.handle}, the profile of agent type {name}, was deleted: it takes no new work"
    role = await repository.role_of(conn, row.run_as_id, enabled.project_id)
    if not holds(role, "writer"):
        return 409, (
            f"agent type {name} runs as {row.run_as}, who holds {role or 'no grant'} on project {enabled.project} now, "
            "not writer: an admin of the project names another member"
        )
    ended = await ended_refusal(conn, row.run_as_id, row.run_as, what=f"a task of agent type {name}")
    return None if ended is None else (403, ended)


def _title(body: TaskIn, name: str) -> str:
    return runs.step_title({"title": body.title or body.task}) or f"task of agent type {name}"


def _bases_refusal(body: TaskIn) -> tuple[int, str] | None:
    """(422, why) the task's base or a base one of its repos names is no branch a task may start from, or None."""
    problem = task_runs.base_problem(body.base) if body.base is not None else None
    if problem is not None:
        return 422, problem
    for repo, base in body.own_bases().items():
        problem = task_runs.base_problem(base)
        if problem is not None:
            return 422, f"the base of {repo}: {problem}"
    return None


async def branch_refusal(
    conn: AsyncConnection, project_id: int, names: list[str], body: TaskIn
) -> tuple[int, str] | None:
    """(status, why) the task of ``body`` may not work on the repos and the branches it names, or None (none named
    included): its repos are some of ``names``, the project's, and its base and the base each of its repos names one a
    task may start from (``task_runs.base_problem``); the repo whose branch it names is one of ``names``, the branch
    one a task may name (``task_runs.branch_problem``), and no task run of the project that is not over works on
    it."""
    unknown = [name for name in body.repo_names() or () if name not in names]
    if unknown:
        return 422, f"the project has no repo {unknown[0]}: a task names some of {', '.join(names) or 'none'}"
    refusal = _bases_refusal(body)
    if refusal is not None or body.repo is None or body.branch is None:
        return refusal
    if body.repo not in names:
        return 422, f"the project has no repo {body.repo}: a task names one of {', '.join(names) or 'none'}"
    problem = task_runs.branch_problem(body.branch, await repository.default_branch(conn, project_id, body.repo))
    if problem is not None:
        return 422, problem
    found = await repository.busy_on(conn, project_id, body.repo, body.branch)
    if found is not None:
        return 409, f"task run #{found} works on {body.branch} of {body.repo} now: the task waits for it to end"
    return None


def _entry(repo: str, branch: str, base: str | None) -> dict:
    """The runs.repos entry of ``repo`` of a task run: ``{repo, branch}``, with ``base`` when the task names one for
    it (``RunRepo``)."""
    return {"repo": repo, "branch": branch, **({"base": base} if base is not None else {})}


async def _queue(
    conn: AsyncConnection, enabled, row, body: TaskIn, left: float, names: list[str], for_id: int | None = None
) -> int:
    """Queue the task run of ``body`` for agent type ``row`` on the branch task/<its id> of each repo of ``names``,
    the project's, or of those of them the task names, but the repo whose branch the task names, on that one, each
    from the base the task names for it, else from the task's base when it names one (``TaskIn.base_of``); its
    principal its run_as, or the person ``for_id`` it is for, bounded by the run_as too (``run_autonomy``)."""
    asked = body.repo_names()
    if asked is not None:
        names = [name for name in names if name in asked]
    profile = await by_id(conn, row.profile_id)
    allow = profiles.allowed(profile.permissions, "model")  # pyright: ignore[reportOptionalMemberAccess]
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
        "timeout_s": task_runs.run_timeout(body.minutes),
        "profile_id": profile.id,  # pyright: ignore[reportOptionalMemberAccess]
        "profile_version": profile.version,  # pyright: ignore[reportOptionalMemberAccess]
        "budget": task_runs.run_budget(left, body.minutes),
        "request": body.task,
        "agent_type_id": row.id,
    }
    also = () if for_id is None else (row.run_as_id,)
    run_id = await insert_run(conn, values, run_autonomy.For(user_id=for_id, also=also, repos=tuple(names)))
    branch = task_runs.branch_of(run_id)
    named = {body.repo: body.branch} if body.repo and body.branch else {}
    repos = [_entry(name, named.get(name, branch), body.base_of(name)) for name in names]
    await repository.set_branches(conn, run_id, branch, repos)
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


def _own_task(task: str, title: str | None, repo: str | None, branch: str | None, minutes: int | None):
    """(the ``TaskIn`` of a core plugin's task, None), or (None, why it is no task)."""
    try:
        body = TaskIn(
            task=runs.clip(task, task_runs.MAX_TASK_BYTES),
            title=(title or "")[: task_runs.MAX_TITLE_CHARS] or None,
            repo=repo,
            branch=branch,
            minutes=minutes,
            repos=None,
            base=None,
        )
    except ValidationError as exc:
        return None, "; ".join(str(error["msg"]) for error in exc.errors())
    return body, None


async def hand_own_task(
    conn: AsyncConnection,
    plugin: str,
    project_id: int,
    name: str,
    task: str,
    title: str | None = None,
    for_id: int | None = None,
    *,
    repo: str | None = None,
    branch: str | None = None,
    minutes: int | None = None,
) -> tuple[int | None, str | None]:
    """A core plugin hands ``task`` to its own agent type ``name`` in project ``project_id``, in the caller's
    transaction: (the task run, None), or (None, why it was not dispatched) for each refusal of the API's route but
    the token's and the grant's. ``task`` is cut to MAX_TASK_BYTES; ``for_id``, the person the run is for (``_queue``);
    ``repo`` and ``branch``, the branch of one repo the run works on, and ``minutes``, its agent time (``TaskIn``).
    A request that carries a run's token dispatches none (``run_access_rules.new_run_refusal``).
    """
    refusal = run_access_rules.new_run_refusal()
    if refusal is not None:
        return None, refusal
    enabled = await plugin_calls.enabled_in(conn, plugin, project_id)
    if enabled is None:
        return None, f"plugin {plugin} is not enabled in the project, or it is turned off"
    row = await enabled_type(conn, project_id, enabled.id, name, lock=True)  # one dispatch of a type at once
    refused = await type_refusal(conn, enabled, row, name)
    if refused is not None:
        return None, refused[1]
    left, problem = await type_left(conn, row)
    if problem is not None:
        return None, problem
    body, problem = _own_task(task, title, repo, branch, minutes)
    if body is None:
        return None, problem
    names = await project_repos(conn, project_id)
    refused = await branch_refusal(conn, project_id, names, body)
    if refused is not None:
        return None, refused[1]
    run_id = await _queue(conn, enabled, row, body, left, names, for_id)
    await _audited(conn, enabled, row, run_id)
    log.info(
        "task dispatched", extra={"project_id": project_id, "plugin": plugin, "agent_type": name, "run_id": run_id}
    )
    return run_id, None


async def hand_again(conn: AsyncConnection, run_id: int, why: str, worker_id: int | None = None):
    """Hand failed task run ``run_id`` again for its plugin, in the caller's transaction, as tier 0 of the operations
    queue does for a run that failed because of its machine (``ops_rules``): (the next attempt, None), or (None, why
    not) while the plugin is not enabled with runs:dispatch, its agent type takes no task (``type_refusal``) or spent
    its cap. The attempt keeps the task's id (its branch), so the plugin hears task.result of it; one pinned to a
    worker is pinned to ``worker_id`` instead, a worker the caller found fit."""
    found = await repository.task_type_of(conn, run_id)
    if found is None:
        return None, f"run {run_id} is no task run of an agent type"
    enabled = await repository.enabled_in_project(conn, found.plugin_id, found.project_id)
    if enabled is None or task_runs.DISPATCH_PERMISSION not in plugin_calls.effective_grant(enabled):
        return None, f"the plugin of agent type {found.name} is not enabled with {task_runs.DISPATCH_PERMISSION}"
    row = await enabled_type(conn, found.project_id, found.plugin_id, found.name, lock=True)
    refusal = await type_refusal(conn, enabled, row, found.name)
    if refusal is not None:
        return None, refusal[1]
    left, problem = await type_left(conn, row)
    if problem is not None:
        return None, problem
    new_id = await repository.queue_attempt(conn, run_id, max(left, 0.0), worker_id)  # the rule's, not the old run's
    await write_event(conn, new_id, {"text": f"the hub hands task run #{run_id} again: {why}", "again_of": run_id})
    await notify_queued(conn, new_id)
    await _audited(conn, enabled, row, new_id)
    return new_id, None


async def _dispatch(conn: AsyncConnection, authorization: str | None, project: str, name: str, body: TaskIn):
    """(the agent type's row, the run's row of ``task_select``, its cap's left, what the type spent) of a dispatch."""
    plugin_id = await plugin_of_authorization(conn, authorization)
    enabled = await dispatcher(conn, plugin_id, project)
    row = await enabled_type(conn, enabled.project_id, plugin_id, name, lock=True)  # one dispatch of a type at once  # pyright: ignore[reportOptionalMemberAccess]
    refusal = await type_refusal(conn, enabled, row, name)
    if refusal is not None:
        raise HTTPException(*refusal)
    left, problem = await type_left(conn, row)
    if problem is not None:
        raise HTTPException(429, problem)
    names = await project_repos(conn, enabled.project_id)  # pyright: ignore[reportOptionalMemberAccess]
    refusal = await branch_refusal(conn, enabled.project_id, names, body)  # pyright: ignore[reportOptionalMemberAccess]
    if refusal is not None:
        raise HTTPException(*refusal)
    run_id = await _queue(conn, enabled, row, body, left, names)
    await _audited(conn, enabled, row, run_id)
    found = await repository.task_by_run(conn, run_id)
    return enabled, row, found, left, await type_spend(conn, row.id)  # pyright: ignore[reportOptionalMemberAccess]


async def dispatch_task(
    engine: AsyncEngine, authorization: str | None, project: str, name: str, body: TaskIn
) -> TaskQueued:
    """Hand a task to agent type ``name`` of the plugin whose token ``authorization`` carries: a run of kind task."""
    if len(body.task.encode()) > task_runs.MAX_TASK_BYTES:
        raise HTTPException(422, f"the task is longer than {task_runs.MAX_TASK_BYTES} bytes in UTF-8")
    async with UnitOfWork(engine) as conn:
        enabled, row, found, left, spent = await _dispatch(conn, authorization, project, name, body)
    budget = task_runs.run_budget(left, body.minutes)
    log.info(
        "task dispatched",
        extra={"project": project, "plugin": enabled.name, "agent_type": name, "run_id": found.id},  # pyright: ignore[reportOptionalMemberAccess]
    )
    return TaskQueued(**task_view(found), budget=TaskBudget(**budget), spent_usd=spent, daily_usd=row.daily_usd)  # pyright: ignore[reportOptionalMemberAccess]


async def show_task(engine: AsyncEngine, authorization: str | None, project: str, task: int) -> Task:
    """A task the plugin whose token ``authorization`` carries handed, as its latest run has it."""
    async with UnitOfWork(engine) as conn:
        plugin_id = await plugin_of_authorization(conn, authorization)
        enabled = await dispatcher(conn, plugin_id, project)
        found = await repository.latest_task(conn, enabled.project_id, plugin_id, task_runs.branch_of(task))  # pyright: ignore[reportOptionalMemberAccess]
    if found is None:
        raise HTTPException(404, f"the plugin handed no task {task} in project {project}")
    return Task(**task_view(found))


# At the claim and at the end


async def task_prompt(conn: AsyncConnection, view) -> str:
    """The prompt of task run ``view`` (``runs.service.views.run_view``) as its worker claims it: a plugin's agent
    type's, or a member's task with no agent type (``member_tasks``)."""
    found = await repository.prompt_parts(conn, view.id)
    profile = await by_id(conn, found.profile_id)
    agent = {
        "handle": profile.handle,  # pyright: ignore[reportOptionalMemberAccess]
        "name": profile.name,  # pyright: ignore[reportOptionalMemberAccess]
        "project": profile.project,  # pyright: ignore[reportOptionalMemberAccess]
        "description": profile.description,  # pyright: ignore[reportOptionalMemberAccess]
    }
    if found.name is None:
        given = await repository.member_task_given(conn, task_runs.task_of(view.branch) or view.id)
        return task_runs.build_member_task_prompt(
            agent=agent,
            login=view.dispatched_by,
            task=view.request or "",
            data=dict(given.input) if given is not None else {},
            assets=list(given.assets) if given is not None else [],
        )
    return task_runs.build_task_prompt(
        agent=agent,
        plugin=found.plugin,
        agent_type=found.name,
        login=view.dispatched_by,
        branch=view.branch or task_runs.branch_of(view.id),
        task=view.request or "",
        repos=[repo.model_dump() for repo in view.repos or []],
    )


async def task_run_ended(conn: AsyncConnection, run_id: int, new: str) -> int:
    """Store the event task.result of task run ``run_id``, moved to ``new``, for the plugin of its agent type, when
    ``new`` ends the task (``task_runs.RESULT_STATES``); how many deliveries. A member's task with no agent type has
    no plugin: its event goes to the project's webhooks alone (``webhook_events``)."""
    if new not in task_runs.RESULT_STATES:
        return 0
    row = await repository.ended_row(conn, run_id)
    if row.agent_type_id is None:
        payload = {
            "task_id": task_runs.task_of(row.branch) or run_id,
            "run_id": run_id,
            "attempt": row.attempt,
            "project": row.name,
            "state": new,
            "failure_cause": row.failure_cause,
        }
        return await webhook_events.enqueue(conn, task_runs.RESULT_EVENT, row.project_id, payload)
    found = await repository.task_by_run(conn, run_id)
    payload = {key: value for key, value in task_view(found).items() if key not in ("queued_at", "finished_at")}
    payload["state"] = new
    return await plugin_events.emit_task_result(conn, found.project_id, found.plugin_id, payload)


# The pull request of a task run's branch


def _pushed_branch(run_id: int, row, body: TaskPushed) -> None:
    """422 when ``body`` names a repo the run does not work in, or a branch of it the run does not push."""
    entries = {entry.get("repo"): entry.get("branch") for entry in row.repos or [] if isinstance(entry, dict)}
    if body.repo not in entries:
        raise HTTPException(422, f"run {run_id} does not work in {body.repo}")
    own = entries[body.repo] if entries[body.repo] != row.branch else None  # the branch its task names for the repo
    if own is not None:
        fits = body.branch == own and task_runs.branch_problem(own) is None
    else:
        fits = body.branch == row.branch and task_runs.is_task_branch(body.branch)
    if not fits:
        raise HTTPException(422, f"run {run_id} pushes {own or row.branch} of {body.repo}, not {body.branch}")


async def _pushed_run(conn: AsyncConnection, user, run_id: int, body: TaskPushed):
    """(the run's title, the repo's origin, the base its task names or None, the repo's default branch) of a push of
    the task run ``run_id`` the worker reports; 404 or 422 when it is not the run's."""
    worker_id = (await worker_of(conn, user))[0]
    row = await repository.pushed_row(conn, run_id)
    if row is None or row.worker_id != worker_id or row.state not in runs.HELD_STATES:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    if row.kind != "task":
        raise HTTPException(404, f"run {run_id} is a {row.kind} run: only a task run's branch gets a pull request here")
    _pushed_branch(run_id, row, body)
    held = [entry for entry in row.repos or [] if isinstance(entry, dict) and entry.get("repo") == body.repo]
    base = held[0].get("base") if held else None
    repo = await repository.repo_origin(conn, row.project_id, body.repo)
    origin, default = (repo.origin, repo.default_branch) if repo else (None, None)
    return row.title, origin, base if isinstance(base, str) else None, default


async def _keep(conn: AsyncConnection, run_id: int, pull: TaskPull, entry: dict | None) -> None:
    """Keep ``entry``, the pull request GitHub opened or had open (``draft_pulls.kept``), in runs.pulls of run
    ``run_id`` (one for each repo), and say what happened in the run's log."""
    kept = {"repo": pull.repo, "url": pull.url, "number": pull.number}
    said = f"the hub opened the draft pull request {pull.url} of {pull.branch} of {pull.repo}; it is never merged"
    if pull.number is None:
        said = f"the hub opened no pull request of {pull.branch} of {pull.repo}: {pull.problem or 'no GitHub App'}"
    elif not pull.opened:
        said = f"the draft pull request {pull.url} of {pull.branch} of {pull.repo} was open already"
    if entry is not None:
        await draft_rows.keep(conn, run_id, entry)
    await write_event(conn, run_id, {"text": said, "pull": kept})


async def _open_pull(app, target, run_id: int, body: TaskPushed, pushed: tuple, policy):
    """(url, number, opened, problem, made) of the draft pull request the workers' App ``app`` opens, or finds open,
    of the pushed branch in GitHub repo ``target``; ``pushed`` is (title, base, default, agent_type, plugin, signed)."""
    title, base, default, agent_type, plugin, signed = pushed
    text = task_runs.DRAFT_TEXT.format(agent_type=agent_type, plugin=plugin, run=run_id) + signed
    try:
        where = f"github.com/{target[0]}/{target[1]}"
        title, text = guard.scrub(policy, where, (title or body.branch)[:240], text)
        made = await draft_pulls.open_for(
            app, target, body.branch, body.head, title, text, base=base or body.base, defaults=(default,)
        )
    except (GitHubRefused, GitHubUnavailable) as exc:
        log.warning("task run pull request not opened", extra={"run_id": run_id, "repo": body.repo, "why": str(exc)})
        return None, None, False, str(exc), None
    return (made.pull or {}).get("html_url"), (made.pull or {}).get("number"), made.opened, made.nothing, made


async def pushed(engine: AsyncEngine, github_app, user, run_id: int, body: TaskPushed) -> TaskPull:
    """The worker pushed the task run's branch of a repo: its draft pull request, opened with the workers' App
    ``github_app`` between two transactions, kept on the run."""
    async with UnitOfWork(engine) as conn:
        title, origin, base, default = await _pushed_run(conn, user, run_id, body)
        agent_type, plugin = await repository.type_and_plugin(conn, run_id)
        signed = await trailers.block(conn, run_id)
        policy = await guard.policy(conn)
    target = github_repo(origin) if origin else None
    url, number, opened, problem, made = None, None, False, None, None
    if target is not None and github_app is not None:
        parts = (title, base, default, agent_type, plugin, signed)
        url, number, opened, problem, made = await _open_pull(github_app, target, run_id, body, parts, policy)
    elif target is not None:
        url = f"https://github.com/{target[0]}/{target[1]}/tree/{body.branch}"
    pull = TaskPull(repo=body.repo, branch=body.branch, url=url, number=number, opened=opened, problem=problem)
    async with UnitOfWork(engine) as conn:
        await _keep(conn, run_id, pull, draft_pulls.kept(body.repo, body.branch, body.head, made) if made else None)
    log.info("task run branch pushed", extra={"run_id": run_id, "repo": body.repo, "pull": number, "opened": opened})
    return pull
