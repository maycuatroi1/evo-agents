"""Dispatching: the runs of a plan's steps (POST /v1/projects/{p}/runs), a plan run (POST .../plan-runs) and an author
run (POST .../author-runs), and what each refuses: a caller without writer, a worker not the caller's or one that could
never claim the run, a repo without an origin, a step not ready, and a plan the Curator made. The night shift queues
its runs through ``insert_run`` and ``unfit`` too (``evo_agents.hub.server.curator``, ``collect``, ``changes``)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg
from fastapi import HTTPException
from sqlalchemy import exists, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import author, runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.credentials import dispatch_credential
from evo_agents.hub.plans import PlanProblem, step_index, step_key
from evo_agents.hub.server import audit
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.run_state import notify_queued
from evo_agents.hub.server.runs.models import ActiveRun, AuthorRunDispatch, Dispatch, PlanRunDispatch, Run, available
from evo_agents.hub.server.runs.service.audits import audit_run, run_target
from evo_agents.hub.server.runs.service.views import (
    Activity,
    busy,
    lock_plan,
    plan_activity,
    plan_busy,
    run_view,
    run_views,
)
from evo_agents.hub.server.security import WEB, Principal

log = logging.getLogger("evo_agents.hub.server.runs")  # the package's one logger, as before it was split


def check_dispatcher(access: ProjectAccess) -> None:
    if not has_role(access.role, "writer"):
        held = access.role or "no grant"
        raise HTTPException(
            403, f"dispatching runs in project {access.name} needs the writer role on it; you hold {held}"
        )


@dataclass(frozen=True)
class Pinned:
    """The worker a dispatch pins its runs to, as its last heartbeat reported it."""

    id: int
    name: str
    runtimes: dict
    checkouts: dict
    agent_version: str | None
    run_kinds: tuple[str, ...] = ()  # the kinds of run its daemon said it runs


async def pinnable(conn: AsyncConnection, user: Principal, access: ProjectAccess, worker_id: int) -> Pinned:
    """403 unless worker ``worker_id`` is ``user``'s own, and unless ``user`` dispatches from a web session when the
    worker takes runs dispatched from the web only; 409 when it is revoked or does not serve the project."""
    w, wp = tables.workers, tables.worker_projects
    serves = exists().where(wp.c.worker_id == w.c.id, wp.c.project_id == access.project_id)
    query = select(
        w.c.owner_id,
        w.c.name,
        w.c.revoked_at,
        serves.label("serves"),
        w.c.runtimes,
        w.c.checkouts,
        w.c.agent_version,
        w.c.dispatch_from,
        w.c.run_kinds,
    ).where(w.c.id == worker_id)
    row = (await conn.execute(query)).one_or_none()
    if row is None or row.owner_id != user.user_id:  # the same answer for another member's worker and for no worker
        raise HTTPException(
            403, f"a run goes only to a worker of the member who dispatches it, and you have no worker {worker_id}"
        )
    name = row.name
    if row.revoked_at is not None:
        raise HTTPException(409, f"worker {name} was revoked at {row.revoked_at.isoformat()}; it takes no runs")
    if not row.serves:
        raise HTTPException(409, f"worker {name} does not take runs of project {access.name}: register it for it")
    if row.dispatch_from == "web" and user.kind != WEB:
        raise HTTPException(
            403,
            f"worker {name} takes only runs dispatched from a web session, as its owner set it, so a token cannot "
            "hand it work: dispatch on the web, or to another worker; nothing was dispatched",
        )
    kinds = tuple(row.run_kinds or ())
    return Pinned(worker_id, name, row.runtimes or {}, row.checkouts or {}, row.agent_version, kinds)


def unfit(worker: Pinned, project: str, kind: str, repos: list[str], runtime: str) -> list[str]:
    """What keeps ``worker`` from ever claiming a run of ``kind`` over ``repos`` asking for ``runtime``, as its last
    heartbeat reported it and the claim checks it; empty when nothing does."""
    problems = []
    if kind == "plan" and not runs.takes_plan_runs(worker.agent_version):
        problems.append(
            f"it runs evo-agents {worker.agent_version or 'of an unknown version'}, and a plan run needs "
            f"{runs.version_text(runs.PLAN_RUN_AGENT)} or later: upgrade it and restart its daemon"
        )
    if (kind in runs.CURATOR_KINDS or kind == "author") and kind not in worker.run_kinds:
        problems.append(
            f"its daemon (evo-agents {worker.agent_version or 'of an unknown version'}) does not say it runs {kind} "
            "runs: upgrade it and restart its daemon"
        )
    usable = [name for name in runs.RUNTIMES if available(worker.runtimes.get(name))]
    if runtime == "any" and not usable:
        problems.append("it reports no runtime available")
    elif runtime != "any" and runtime not in usable:
        problems.append(f"it does not report {runtime} available")
    missing = [f"{project}/{repo}" for repo in repos if f"{project}/{repo}" not in worker.checkouts]
    if missing:
        problems.append(
            f"it has no checkout of {', '.join(missing)}: clone each one where the harness registry or the "
            "project's workspace places it, or name its path in checkouts of the worker's config.json, then restart "
            "its daemon"
        )
    return problems


def _fits(worker: Pinned | None, access: ProjectAccess, kind: str, repos: list[str], runtime: str) -> None:
    """409 when the run is pinned to a worker that cannot claim it, rather than a run that stays queued for good."""
    if worker is None:
        return
    problems = unfit(worker, access.name, kind, repos, runtime)
    if problems:
        raise HTTPException(
            409,
            f"worker {worker.name} cannot take this run: {'; '.join(problems)}. Dispatch it again once the worker's "
            "heartbeat reports that, or dispatch it to another worker; nothing was dispatched",
        )


def _short(value) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


async def _refuse_without_origin(conn: AsyncConnection, access: ProjectAccess, repos: list[str], what: str) -> None:
    """409 when the project lists no origin for one of ``repos`` (no repo of that name, or one without its origin): no
    credential is leased for such a repo, and the worker's preflight would fail the run before its agent starts."""
    pr = tables.project_repos
    listed = await conn.execute(
        select(pr.c.name, pr.c.origin).where(pr.c.project_id == access.project_id, pr.c.name.in_(repos))
    )
    origins = {name: origin for name, origin in listed}
    unlisted = [name for name in dict.fromkeys(repos) if not origins.get(name)]
    if unlisted:
        names = ", ".join(unlisted)
        raise HTTPException(
            409,
            f"{what} needs {names}, for which project {access.name} lists no origin: add the repo with its origin to "
            "the harness and run `evo-agents hub project register` again; nothing was dispatched",
        )


async def insert_run(conn: AsyncConnection, values: dict) -> int:
    """Insert a run with the column ``values`` in a savepoint and return its id. When the database refuses the row,
    the savepoint is rolled back and the IntegrityError, with the psycopg error that says why in ``orig``, goes on to
    the caller, whose transaction stays usable."""
    r = tables.runs
    async with conn.begin_nested():
        return (await conn.execute(insert(r).values(**values).returning(r.c.id))).scalar_one()


async def queue_run(
    conn: AsyncConnection,
    access: ProjectAccess,
    user: Principal,
    held,
    key: str,
    active: dict[str, ActiveRun],
    *,
    runtime: str,
    model: str | None,
    mode: str,
    approval: str,
    timeout_s: int,
    pinned: Pinned | None,
    parent: int | None = None,
) -> int:
    """Queue a run of step ``key`` of the plan ``held``, in the caller's transaction; HTTPException when the step
    may not run now."""
    body = held.body
    try:
        step = body["steps"][step_index(body, key)]
    except PlanProblem as exc:
        raise HTTPException(422, f"{exc}; nothing was dispatched") from None
    reason = runs.unready_reason(body, step)
    if reason is None and key in active:
        reason = busy(active[key])
    if reason is not None:
        raise HTTPException(409, f"step {key} of plan {held.plan_id} is not ready: {reason}; nothing was dispatched")
    repo = runs.plan_repo(body, step) or {}
    name = _short(repo.get("repo"))
    if name is None:
        raise HTTPException(
            422, f"step {key} names no repo and the plan does not list exactly one: give the step a repo"
        )
    _fits(pinned, access, "step", [name], runtime)
    await _refuse_without_origin(conn, access, [name], f"step {key} of plan {held.plan_id}")
    values = {
        "project_id": access.project_id,
        "plan_id": held.plan_id,
        "step_key": key,
        "title": runs.step_title(step),
        "plan_revision": held.revision,
        "dispatched_by": user.user_id,
        "dispatched_via": dispatch_credential(user.kind),
        "pinned_worker_id": None if pinned is None else pinned.id,
        "requested_runtime": runtime,
        "runtime": runtime,
        "model": model,
        "mode": mode,
        "approval": approval,
        "timeout_s": timeout_s,
        "parent_run_id": parent,
        "repo": name,
        "branch": _short(repo.get("branch")),
    }
    try:
        run_id = await insert_run(conn, values)
    except IntegrityError as exc:
        if isinstance(exc.orig, psycopg.errors.UniqueViolation):  # another dispatch of the step got in first
            raise HTTPException(409, f"step {key} of plan {held.plan_id} has an active run already") from None
        if isinstance(exc.orig, psycopg.errors.CheckViolation):
            raise HTTPException(422, f"step {key}: its repo or branch name is not one a run can hold") from None
        raise
    active[key] = ActiveRun(id=run_id, state="queued", dispatched_by=user.login)
    await notify_queued(conn, run_id)
    return run_id


async def refuse_curator_plan(conn: AsyncConnection, access: ProjectAccess, plan_id: str) -> None:
    """409 for a plan the Curator made: the night shift alone runs it (``changes.refuse_manual_dispatch``)."""
    from evo_agents.hub.server.changes import refuse_manual_dispatch  # it queues runs through this package

    await refuse_manual_dispatch(conn, access.project_id, plan_id)


def no_plan_run(activity: Activity, plan_id: str) -> None:
    """409 while the plan has an active plan run: its steps are that run's until it ends."""
    if activity.plan_run is not None:
        raise HTTPException(
            409,
            f"{plan_busy(plan_id, activity.plan_run)}: its steps are that run's until it ends; nothing was dispatched",
        )


async def dispatch_steps(engine, user: Principal, project: str, body: Dispatch) -> list[Run]:
    """Queue a run of each step named, all of them or none; each one goes to a worker of the caller."""
    keys = list(dict.fromkeys(str(step) for step in body.steps))
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        check_dispatcher(access)
        held = await plan_routes._visible(conn, access, body.plan_id, None)
        await refuse_curator_plan(conn, access, body.plan_id)
        pinned = None if body.worker_id is None else await pinnable(conn, user, access, body.worker_id)
        await lock_plan(conn, access.project_id, body.plan_id)
        activity = await plan_activity(conn, access.project_id, body.plan_id)
        no_plan_run(activity, body.plan_id)
        queued = []
        for key in keys:
            run_id = await queue_run(
                conn,
                access,
                user,
                held,
                key,
                activity.steps,
                runtime=body.runtime,
                model=body.model,
                mode=body.mode,
                approval=body.approval,
                timeout_s=body.timeout_min * 60,
                pinned=pinned,
            )
            target = run_target(project, body.plan_id, key, run_id)
            await audit.record(
                conn,
                actor_id=user.user_id,
                token_id=user.token_id,
                action=audit.RUN_DISPATCH,
                target=target,
                project_id=access.project_id,
            )
            queued.append(run_id)
        views = await run_views(conn, queued)
    log.info(
        "runs dispatched",
        extra={"project": project, "plan_id": body.plan_id, "runs": queued, "login": user.login},
    )
    return views


def _pending(step) -> bool:
    """Whether a step counts as pending: a status of pending or none, or a bare string, which has none."""
    return not isinstance(step, dict) or step.get("status") in (None, "pending")


def plan_run_repos(held) -> list[dict]:
    """The repos of a plan run of the plan ``held``: the repo of each step not done, in plan order, each once with the
    branch the plan's repos name for it. 409 when no step is pending, or a step not done names no repo and the plan
    does not list exactly one."""
    body = held.body
    steps = body.get("steps") if isinstance(body.get("steps"), list) else []
    open_steps = [
        (step_key(step, index), step)
        for index, step in enumerate(steps)
        if not (isinstance(step, dict) and step.get("status") == "done")
    ]
    if not any(_pending(step) for _, step in open_steps):
        raise HTTPException(
            409, f"plan {held.plan_id} has no pending step, so a plan run has nothing to do; nothing was dispatched"
        )
    repos: dict[str, dict] = {}
    for key, step in open_steps:
        entry = runs.plan_repo(body, step if isinstance(step, dict) else {}) or {}
        name = _short(entry.get("repo"))
        if name is None:
            raise HTTPException(
                409,
                f"step {key} of plan {held.plan_id} is not done and names no repo, and the plan does not list exactly "
                "one: give the step a repo; nothing was dispatched",
            )
        repos.setdefault(name, {"repo": name, "branch": _short(entry.get("branch"))})
    return list(repos.values())


async def dispatch_plan_run(engine, user: Principal, project: str, body: PlanRunDispatch) -> Run:
    """Queue a plan run: one run, on a worker of the caller's, that does every step of the plan not done yet."""
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        check_dispatcher(access)
        held = await plan_routes._visible(conn, access, body.plan_id, None)
        await refuse_curator_plan(conn, access, body.plan_id)
        pinned = None if body.worker_id is None else await pinnable(conn, user, access, body.worker_id)
        await lock_plan(conn, access.project_id, body.plan_id)
        activity = await plan_activity(conn, access.project_id, body.plan_id)
        if activity.plan_run is not None:
            raise HTTPException(409, f"{plan_busy(body.plan_id, activity.plan_run)}; nothing was dispatched")
        if activity.steps:
            key, active = min(activity.steps.items(), key=lambda item: item[1].id)
            busy = f"has run #{active.id}, {active.state}, dispatched by {active.dispatched_by}"
            raise HTTPException(
                409,
                f"step {key} of plan {body.plan_id} {busy}: a plan run waits until no run of the plan's steps is "
                "active; nothing was dispatched",
            )
        repos = plan_run_repos(held)
        _fits(pinned, access, "plan", [entry["repo"] for entry in repos], body.runtime)
        await _refuse_without_origin(conn, access, [entry["repo"] for entry in repos], f"plan {held.plan_id}")
        values = {
            "kind": "plan",
            "project_id": access.project_id,
            "plan_id": held.plan_id,
            "title": runs.step_title(held.body),
            "plan_revision": held.revision,
            "dispatched_by": user.user_id,
            "dispatched_via": dispatch_credential(user.kind),
            "pinned_worker_id": body.worker_id,
            "requested_runtime": body.runtime,
            "runtime": body.runtime,
            "model": body.model,
            "mode": body.mode,
            "approval": "auto",
            "timeout_s": body.timeout_h * 3600,
            "repos": repos,
        }
        try:
            run_id = await insert_run(conn, values)
        except IntegrityError as exc:
            if isinstance(exc.orig, psycopg.errors.UniqueViolation):  # another plan run of the plan got in first
                busy = f"plan {held.plan_id} has an active plan run already; nothing was dispatched"
                raise HTTPException(409, busy) from None
            if isinstance(exc.orig, psycopg.errors.CheckViolation):
                raise HTTPException(
                    422,
                    f"plan {held.plan_id}: a repo or branch name, or the number of repos, is not one a run can hold",
                ) from None
            raise
        await notify_queued(conn, run_id)
        target = run_target(project, held.plan_id, None, run_id)
        await audit_run(conn, user, access, audit.RUN_DISPATCH_PLAN, target)
        view = await run_view(conn, run_id)
    log.info(
        "plan run dispatched",
        extra={"project": project, "plan_id": held.plan_id, "run_id": run_id, "repos": len(view.repos or [])},
    )
    return view


async def _author_repos(conn: AsyncConnection, access: ProjectAccess, worker: Pinned) -> list[dict]:
    """The repos of an author run on ``worker``: the project's harness first, where plans live, then each repo of the
    project the worker has a checkout of, in name order, each with no branch, since an author run's worktrees are
    detached. 409 when the project was registered without its harness."""
    p, pr = tables.projects, tables.project_repos
    path = (await conn.execute(select(p.c.harness_path).where(p.c.id == access.project_id))).scalar_one_or_none()
    harness = author.harness_repo(path)
    if harness is None:
        raise HTTPException(
            409,
            f"project {access.name} was registered without its harness, where plans live, so an author run has "
            "nowhere to read the project's plans: register it again with `evo-agents hub project register` from its "
            "harness; nothing was dispatched",
        )
    names = await conn.execute(select(pr.c.name).where(pr.c.project_id == access.project_id).order_by(pr.c.name))
    repos = [{"repo": harness, "branch": None}]
    for name in names.scalars():
        if name != harness and f"{access.name}/{name}" in worker.checkouts:
            repos.append({"repo": name, "branch": None})
    return repos


async def dispatch_author_run(engine, user: Principal, project: str, body: AuthorRunDispatch) -> Run:
    """Queue an author run: a plan written from the caller's request, with create-exec-plan, on a worker of the
    caller's (``evo_agents.hub.author``)."""
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        check_dispatcher(access)
        problem = author.request_problem(body.request) or author.runtime_problem(body.runtime)
        if problem:
            raise HTTPException(422, f"{problem}; nothing was dispatched")
        runtime = author.AUTHOR_RUNTIMES[0] if body.runtime == "any" else body.runtime
        pinned = await pinnable(conn, user, access, body.worker_id)
        repos = await _author_repos(conn, access, pinned)
        _fits(pinned, access, "author", [repos[0]["repo"]], runtime)
        revision = None
        if body.plan_id is not None:
            held = await plan_routes._visible(conn, access, body.plan_id, None)
            from evo_agents.hub.server.changes import refuse_manual_dispatch  # it queues runs through this package

            await refuse_manual_dispatch(conn, access.project_id, body.plan_id)
            revision = held.revision
        values = {
            "kind": "author",
            "project_id": access.project_id,
            "plan_id": body.plan_id,
            "title": author.author_title(body.request),
            "plan_revision": revision,
            "dispatched_by": user.user_id,
            "dispatched_via": dispatch_credential(user.kind),
            "pinned_worker_id": pinned.id,
            "requested_runtime": runtime,
            "runtime": runtime,
            "model": body.model,
            "mode": "headless",
            "approval": "auto",
            "timeout_s": body.timeout_h * 3600,
            "repos": repos,
            "request": body.request,
        }
        try:
            run_id = await insert_run(conn, values)
        except IntegrityError as exc:
            if isinstance(exc.orig, psycopg.errors.CheckViolation):
                raise HTTPException(
                    422, "a repo name, or the number of repos, is not one a run can hold; nothing was dispatched"
                ) from None
            raise
        await notify_queued(conn, run_id)
        await audit_run(conn, user, access, audit.RUN_DISPATCH_AUTHOR, f"{project}/author run:{run_id}")
        view = await run_view(conn, run_id)
    log.info(
        "author run dispatched",
        extra={
            "project": project,
            "run_id": run_id,
            "worker_id": pinned.id,
            "repos": len(repos),
            "plan_id": body.plan_id,
        },
    )
    return view
