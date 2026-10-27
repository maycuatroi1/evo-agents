"""Dispatching: the runs of a plan's steps (POST /v1/projects/{p}/runs), a plan run (POST .../plan-runs) and an author
run (POST .../author-runs), and what each refuses: a caller without writer, a worker not the caller's or one that could
never claim the run, a repo without an origin, a step not ready, a plan the Curator made, and what the agent profile
the dispatch names (the project's default without one) does not allow (``service.profiles``), and a caller whose
primary seat in the org chart ended (``check_seat``). The night shift queues
its runs through ``insert_run`` and ``unfit`` too (``evo_agents.hub.server.curator``, ``collect``, ``changes``):
``insert_run`` gives a run whose values name no profile the project's default."""

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
from evo_agents.hub.server import audit, autonomy_hold, run_access, run_autonomy, worker_placement
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.org_store import ended_refusal
from evo_agents.hub.server.plugin_configs import check_run_layer
from evo_agents.hub.server.profile_store import ensure_default
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.repo_origins import repo_origins
from evo_agents.hub.server.run_state import notify_queued
from evo_agents.hub.server.runs.models import ActiveRun, AuthorRunDispatch, Dispatch, PlanRunDispatch, Run, available
from evo_agents.hub.server.runs.service import browsers, plugin_runtimes
from evo_agents.hub.server.runs.service.accounts import Picked, pick_account, record_pick
from evo_agents.hub.server.runs.service.audits import audit_run, run_target
from evo_agents.hub.server.runs.service.profiles import Asked, Chosen, choose
from evo_agents.hub.server.runs.service.views import (
    Activity,
    busy,
    lock_plan,
    plan_activity,
    plan_busy,
    run_view,
    run_views,
)
from evo_agents.hub.server.runs.service.worker_fit import shared_in
from evo_agents.hub.server.security import WEB, Principal
from evo_agents.plugins.runtimes import is_plugin_runtime

log = logging.getLogger("evo_agents.hub.server.runs")  # the package's one logger, as before it was split


def check_dispatcher(access: ProjectAccess) -> None:
    if not has_role(access.role, "writer"):
        held = access.role or "no grant"
        raise HTTPException(
            403, f"dispatching runs in project {access.name} needs the writer role on it; you hold {held}"
        )


async def check_seat(conn: AsyncConnection, user: Principal, what: str = "dispatching a run") -> None:
    """403 when the caller's primary seat in the org chart ended, naming its unit and the day (plan org-chart, a5)."""
    reason = await ended_refusal(conn, user.user_id, user.login, what=what)
    if reason is not None:
        raise HTTPException(403, reason)


@dataclass(frozen=True)
class Pinned:
    """The worker a dispatch pins its runs to, as its last heartbeat reported it."""

    id: int
    name: str
    runtimes: dict
    checkouts: dict
    agent_version: str | None
    run_kinds: tuple[str, ...] = ()  # the kinds of run its daemon said it runs
    features: tuple[str, ...] = ()  # what its daemon said it does besides them (runs.HUB_FEATURES)
    labels: tuple[str, ...] = ()  # its owner's labels, which an agent profile's worker axis may name
    plugins: tuple[str, ...] = ()  # the plugins whose runtimes and environments its owner lets it take
    pool: str | None = None  # the pool it is a node of, which an agent profile's worker axis may name


async def pinnable(conn: AsyncConnection, user: Principal, access: ProjectAccess, worker_id: int) -> Pinned:
    """403 unless worker ``worker_id`` is ``user``'s own, or one its owner shares and that serves the project while the
    owner holds writer on it (``docs/workers.md``, Shared workers), and unless ``user`` dispatches from a web session
    when the worker takes runs dispatched from the web only; 409 when it is revoked or does not serve the project."""
    w, wp = tables.workers, tables.worker_projects
    serves = exists().where(wp.c.worker_id == w.c.id, wp.c.project_id == access.project_id)
    query = select(
        w.c.owner_id,
        shared_in(access.project_id, user.user_id).label("shared_here"),
        w.c.name,
        w.c.revoked_at,
        serves.label("serves"),
        w.c.runtimes,
        w.c.checkouts,
        w.c.agent_version,
        w.c.dispatch_from,
        w.c.run_kinds,
        w.c.features,
        w.c.labels,
        w.c.plugins,
        worker_placement.pool_name(w).label("pool"),
    ).where(w.c.id == worker_id)
    row = (await conn.execute(query)).one_or_none()
    if row is None or (row.owner_id != user.user_id and not row.shared_here):  # one answer for both, and for none
        raise HTTPException(
            403,
            f"a run goes only to a worker of the member who dispatches it or to one shared in project {access.name}, "
            f"and worker {worker_id} is neither",
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
    kinds, features = tuple(row.run_kinds or ()), tuple(row.features or ())
    found = (row.runtimes or {}, row.checkouts or {}, row.agent_version, kinds, features, tuple(row.labels or ()))
    return Pinned(worker_id, name, *found, plugins=tuple(row.plugins or ()), pool=row.pool)


async def pinned_with_identities(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, names, pinned: Pinned | None
) -> tuple[list[int], Pinned | None]:
    """The ids of the browser identities ``names`` a dispatch names (``browsers.resolve``) and the worker its run goes
    to: ``pinned``, else the one that holds the identities, as ``pinnable`` checks it for ``user``."""
    ids, worker_id = await browsers.resolve(conn, user, access, names, pinned)
    if ids and pinned is None:
        pinned = await pinnable(conn, user, access, worker_id)
    return ids, pinned


async def with_profile_identity(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    chosen: Chosen,
    identities: list[int],
    pinned: Pinned | None,
) -> tuple[list[int], Pinned | None]:
    """``identities`` and ``pinned`` as they are for a run under the agent profile ``chosen``: the identity the profile
    hands its runs when the dispatch names none, with the run pinned to the worker that holds it."""
    if identities:
        return identities, pinned
    ids, worker_id = await browsers.profile_default(conn, chosen.profile.id, pinned)
    if ids and pinned is None:
        pinned = await pinnable(conn, user, access, worker_id)
    return ids, pinned


async def worker_as_pinned(conn: AsyncConnection, worker_id: int) -> Pinned | None:
    """Worker ``worker_id`` as its last heartbeat reported it, for a run the hub hands out itself in its owner's name
    (the Loop's, ``evo_agents.hub.server.loop.dispatch``): no check of who dispatches; None for no such worker."""
    w = tables.workers
    query = select(
        w.c.name,
        w.c.runtimes,
        w.c.checkouts,
        w.c.agent_version,
        w.c.run_kinds,
        w.c.features,
        w.c.labels,
        w.c.plugins,
        worker_placement.pool_name(w).label("pool"),
    ).where(w.c.id == worker_id)
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return None
    return Pinned(
        worker_id,
        row.name,
        row.runtimes or {},
        row.checkouts or {},
        row.agent_version,
        tuple(row.run_kinds or ()),
        tuple(row.features or ()),
        tuple(row.labels or ()),
        tuple(row.plugins or ()),
        row.pool,
    )


def unfit(
    worker: Pinned,
    project: str,
    kind: str,
    repos: list[str],
    runtime: str,
    *,
    session_per_step: bool = False,
    browser: bool = False,
) -> list[str]:
    """What keeps ``worker`` from ever claiming a run of ``kind`` over ``repos`` asking for ``runtime`` (a plan run
    with a session per step when ``session_per_step``, a run with a Chromium of its own when ``browser``), as its last
    heartbeat reported it and the claim checks it; empty when nothing does. A repo it has no checkout of keeps only an
    older daemon from it: one that says it clones them (runs.CLONES_CHECKOUTS) clones it before the run starts."""
    problems = []
    if kind == "plan" and not runs.takes_plan_runs(worker.agent_version):
        problems.append(
            f"it runs evo-agents {worker.agent_version or 'of an unknown version'}, and a plan run needs "
            f"{runs.version_text(runs.PLAN_RUN_AGENT)} or later: upgrade it and restart its daemon"
        )
    elif session_per_step and runs.SESSION_PER_STEP not in worker.features:
        problems.append(
            f"its daemon (evo-agents {worker.agent_version or 'of an unknown version'}) does not say it runs a plan "
            "run with a session for each step, and would run every step in one session: upgrade it and restart its "
            "daemon, or dispatch the plan run without a session per step"
        )
    if (kind in runs.CURATOR_KINDS or kind in ("author", "chat", "browser")) and kind not in worker.run_kinds:
        problems.append(
            f"its daemon (evo-agents {worker.agent_version or 'of an unknown version'}) does not say it runs {kind} "
            "runs: upgrade it and restart its daemon"
        )
    usable = [name for name in runs.RUNTIMES if available(worker.runtimes.get(name))]
    if runtime == "any" and not usable:
        problems.append("it reports no runtime available")
    elif is_plugin_runtime(runtime):
        problems += plugin_runtimes.unfit(
            runtime,
            worker_id=worker.id,
            agent_version=worker.agent_version,
            features=worker.features,
            plugins=worker.plugins,
            runtimes=worker.runtimes,
        )
    elif runtime != "any" and runtime not in usable:
        problems.append(f"it does not report {runtime} available")
    missing = [f"{project}/{repo}" for repo in repos if f"{project}/{repo}" not in worker.checkouts]
    if missing and runs.CLONES_CHECKOUTS not in worker.features:
        problems.append(
            f"it has no checkout of {', '.join(missing)}, and its daemon (evo-agents "
            f"{worker.agent_version or 'of an unknown version'}) does not clone what it lacks: upgrade it and restart "
            "its daemon"
        )
    return problems + browsers.unfit(worker.features, browser)


def _fits(
    worker: Pinned | None, access: ProjectAccess, kind: str, repos: list[str], runtime: str, **wanted: bool
) -> None:
    """409 when the run is pinned to a worker that cannot claim it, rather than a run that stays queued for good;
    ``wanted`` names what else the run asks of it (``unfit``)."""
    if worker is None:
        return
    problems = unfit(worker, access.name, kind, repos, runtime, **wanted)
    if problems:
        raise HTTPException(
            409,
            f"worker {worker.name} cannot take this run: {'; '.join(problems)}. Dispatch it again once the worker's "
            "heartbeat reports that, or dispatch it to another worker; nothing was dispatched",
        )


def _short(value) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


async def _refuse_without_origin(
    conn: AsyncConnection, access: ProjectAccess, user: Principal, repos: list[str], what: str
) -> None:
    """409 when no origin is known for one of ``repos``, neither in the project nor in another one ``user`` holds a
    grant on (``repo_origins``): no credential is leased for such a repo, a worker without a checkout of it has
    nothing to clone, and the worker's preflight would fail the run before its agent starts."""
    origins = await repo_origins(conn, access.project_id, user.user_id, repos)
    unlisted = [name for name in dict.fromkeys(repos) if name not in origins]
    if unlisted:
        names = ", ".join(unlisted)
        raise HTTPException(
            409,
            f"{what} needs {names}, for which neither project {access.name} nor another project you hold a grant on "
            "lists an origin: add the repo with its origin to the harness of either and run `evo-agents hub project "
            "register` again; nothing was dispatched",
        )


async def insert_run(conn: AsyncConnection, values: dict, who: run_autonomy.For | None = None) -> int:
    """Insert a run with the column ``values`` in a savepoint and return its id; values that name no agent profile
    take the project's default, and every run its principal and autonomy snapshot (``run_autonomy.stamp``, ``who``
    the principal when its kind does not say it). When the database refuses the row, the savepoint is rolled back and the IntegrityError,
    with the psycopg error that says why in ``orig``, goes on to the caller, whose transaction stays usable. A stop of
    autonomy that holds the run's principal refuses it, ``autonomy_hold.Stopped``, a 403, before anything is written."""
    r = tables.runs
    if "profile_id" not in values:
        default = await ensure_default(conn, values["project_id"])
        values = {**values, "profile_id": default.id, "profile_version": default.version}
    values = await run_autonomy.stamp(conn, values, who)
    await autonomy_hold.hold(conn, values.get("principal_id"))
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
    picked: tuple[Picked, ...] = (),
    chosen: Chosen | None = None,
    plugin_config: dict | None = None,
    browser: bool = False,
    identities: list[int] | tuple[int, ...] = (),
    placed: dict | None = None,
    as_owner: bool = False,
) -> int:
    """Queue a run of step ``key`` of the plan ``held``, in the caller's transaction, taking the accounts ``picked``
    by hand, under the agent profile ``chosen`` (the project's default without one), with its own layer of plugin
    config ``plugin_config`` (checked already), with a Chromium of its own when ``browser`` or browser
    ``identities`` (resolved already, the run ``pinned`` to their worker), the selector and tolerations
    ``placed`` (``placement``), and running as its owner when ``as_owner`` (``run_access``, checked already);
    HTTPException when the step may not run now."""
    body = held.body
    browser = browser or bool(identities)
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
    _fits(pinned, access, "step", [name], runtime, browser=browser)
    await _refuse_without_origin(conn, access, user, [name], f"step {key} of plan {held.plan_id}")
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
        "browser": browser,
        "browser_identities": list(identities),
        "as_owner": as_owner,
        **_listed(placed),
        **({} if plugin_config is None else {"plugin_config": plugin_config}),  # a JSON null is no layer
        **({} if chosen is None else chosen.columns()),
    }
    try:
        run_id = await insert_run(conn, values)
    except IntegrityError as exc:
        if isinstance(exc.orig, psycopg.errors.UniqueViolation):  # another dispatch of the step got in first
            raise HTTPException(409, f"step {key} of plan {held.plan_id} has an active run already") from None
        if isinstance(exc.orig, psycopg.errors.CheckViolation):
            raise HTTPException(422, f"step {key}: its repo or branch name is not one a run can hold") from None
        raise
    for account in picked:
        await record_pick(conn, run_id, account)
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


async def _picked(conn: AsyncConnection, user: Principal, access: ProjectAccess, body, pinned) -> Picked | None:
    """The account ``body`` picks by hand for the run, checked (``service.accounts``) against where the run goes;
    None when none is named."""
    if body.account is None:
        return None
    return await pick_account(conn, user, access, body.account, pinned, selector=body.selector())


def _account_text(picked: Picked | None) -> str:
    return "" if picked is None else picked.audit_text()


def _picks(picked: Picked | None) -> tuple[Picked, ...]:
    return () if picked is None else (picked,)


def placement(body) -> dict:
    """The selector and tolerations a dispatch ``body`` (``PlacementFields``) gives its runs, each a tuple: the
    ``Asked`` fields and the ``placed`` of ``queue_run``."""
    return {"selector": tuple(body.selector()), "tolerations": tuple(body.tolerations or ())}


def _listed(placed: dict | None) -> dict:
    """The run columns of ``placed`` (``placement``)."""
    return {name: list(values) for name, values in (placed or {}).items()}


async def dispatch_steps(engine, user: Principal, project: str, body: Dispatch) -> list[Run]:
    """Queue a run of each step named, all of them or none; each one goes to a worker of the caller, and takes the
    account named by hand, if one is."""
    keys = list(dict.fromkeys(str(step) for step in body.steps))
    run_access.refuse_onward(user, body.as_me)
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        check_dispatcher(access)
        await check_seat(conn, user)
        held = await plan_routes._visible(conn, access, body.plan_id, None)
        await refuse_curator_plan(conn, access, body.plan_id)
        pinned = None if body.worker_id is None else await pinnable(conn, user, access, body.worker_id)
        identities, pinned = await pinned_with_identities(conn, user, access, body.identities, pinned)
        picked = await _picked(conn, user, access, body, pinned)
        placed = placement(body)
        asked = Asked("step", body.runtime, body.model, pinned, _picks(picked), **placed)
        chosen = await choose(conn, user, access, body.profile, asked)
        identities, pinned = await with_profile_identity(conn, user, access, chosen, identities, pinned)
        layer = await check_run_layer(conn, access.project_id, project, body.plugin_config)
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
                model=chosen.model,
                mode=body.mode,
                approval=body.approval,
                timeout_s=body.timeout_min * 60,
                pinned=pinned,
                picked=_picks(picked),
                chosen=chosen,
                plugin_config=layer,
                browser=body.browser,
                identities=identities,
                placed=placed,
                as_owner=body.as_me,
            )
            target = run_target(project, body.plan_id, key, run_id) + _account_text(picked) + chosen.audit_text()
            await audit.record(
                conn,
                actor_id=user.user_id,
                token_id=user.token_id,
                action=audit.RUN_DISPATCH,
                target=target,
                project_id=access.project_id,
            )
            if body.as_me:
                granted = f"{run_target(project, body.plan_id, key, run_id)} by=dispatch"
                await run_access.record(conn, user, access.project_id, granted, granted=True)
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
    """Queue a plan run: one run, on a worker of the caller's, that does every step of the plan not done yet, in one
    agent session or, with ``session_per_step``, in a new session for each step; pinned to a worker whose daemon does
    not say it runs that, 409."""
    run_access.refuse_onward(user, body.as_me)
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        check_dispatcher(access)
        await check_seat(conn, user)
        held = await plan_routes._visible(conn, access, body.plan_id, None)
        await refuse_curator_plan(conn, access, body.plan_id)
        pinned = None if body.worker_id is None else await pinnable(conn, user, access, body.worker_id)
        identities, pinned = await pinned_with_identities(conn, user, access, body.identities, pinned)
        browser = body.browser or bool(identities)
        picked = await _picked(conn, user, access, body, pinned)
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
        names = [entry["repo"] for entry in repos]
        _fits(pinned, access, "plan", names, body.runtime, session_per_step=body.session_per_step, browser=browser)
        await _refuse_without_origin(conn, access, user, names, f"plan {held.plan_id}")
        placed = placement(body)
        asked = Asked("plan", body.runtime, body.model, pinned, _picks(picked), **placed)
        chosen = await choose(conn, user, access, body.profile, asked)
        if not identities:
            identities, pinned = await with_profile_identity(conn, user, access, chosen, identities, pinned)
            if identities:
                browser = True
                _fits(pinned, access, "plan", names, body.runtime, session_per_step=body.session_per_step, browser=True)
        values = {
            "kind": "plan",
            "project_id": access.project_id,
            "plan_id": held.plan_id,
            "title": runs.step_title(held.body),
            "plan_revision": held.revision,
            "dispatched_by": user.user_id,
            "dispatched_via": dispatch_credential(user.kind),
            "pinned_worker_id": None if pinned is None else pinned.id,
            "requested_runtime": body.runtime,
            "runtime": body.runtime,
            "model": chosen.model,
            "mode": body.mode,
            "approval": "auto",
            "timeout_s": body.timeout_h * 3600,
            "repos": repos,
            "session_per_step": body.session_per_step,
            "browser": browser,
            "browser_identities": identities,
            "as_owner": body.as_me,
            **_listed(placed),
            **chosen.columns(),
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
        await record_pick(conn, run_id, picked)
        await notify_queued(conn, run_id)
        target = run_target(project, held.plan_id, None, run_id) + _account_text(picked) + chosen.audit_text()
        await audit_run(conn, user, access, audit.RUN_DISPATCH_PLAN, target)
        if body.as_me:
            granted = f"{run_target(project, held.plan_id, None, run_id)} by=dispatch"
            await run_access.record(conn, user, access.project_id, granted, granted=True)
        view = await run_view(conn, run_id)
    log.info(
        "plan run dispatched",
        extra={
            "project": project,
            "plan_id": held.plan_id,
            "run_id": run_id,
            "repos": len(view.repos or []),
            "session_per_step": body.session_per_step,
        },
    )
    return view


async def author_repos(
    conn: AsyncConnection, project_id: int, project: str, checkouts: dict | None
) -> list[dict] | None:
    """The repos of an author run of ``project`` on a worker with ``checkouts``: the project's harness first, where
    plans live, then each repo of the project the worker has a checkout of (every one for ``checkouts`` None, a run
    pinned to no worker, whose worker leaves out those it has no checkout of), in name order, each with no branch,
    since an author run's worktrees are detached. None when the project was registered without its harness."""
    p, pr = tables.projects, tables.project_repos
    path = (await conn.execute(select(p.c.harness_path).where(p.c.id == project_id))).scalar_one_or_none()
    harness = author.harness_repo(path)
    if harness is None:
        return None
    names = await conn.execute(select(pr.c.name).where(pr.c.project_id == project_id).order_by(pr.c.name))
    repos = [{"repo": harness, "branch": None}]
    for name in names.scalars():
        if name != harness and (checkouts is None or f"{project}/{name}" in checkouts):
            repos.append({"repo": name, "branch": None})
    return repos


async def _author_repos(conn: AsyncConnection, access: ProjectAccess, worker: Pinned | None) -> list[dict]:
    """``author_repos`` on ``worker``, or on any for None; 409 when the project was registered without its
    harness."""
    repos = await author_repos(conn, access.project_id, access.name, None if worker is None else worker.checkouts)
    if repos is None:
        raise HTTPException(
            409,
            f"project {access.name} was registered without its harness, where plans live, so an author run has "
            "nowhere to read the project's plans: register it again with `evo-agents hub project register` from its "
            "harness; nothing was dispatched",
        )
    return repos


async def dispatch_author_run(engine, user: Principal, project: str, body: AuthorRunDispatch) -> Run:
    """Queue an author run: a plan written from the caller's request, with create-exec-plan, on the worker of the
    caller's it names, or on any that its selector and tolerations let claim it (``evo_agents.hub.author``)."""
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        check_dispatcher(access)
        await check_seat(conn, user)
        problem = author.request_problem(body.request) or author.runtime_problem(body.runtime)
        if problem:
            raise HTTPException(422, f"{problem}; nothing was dispatched")
        runtime = author.AUTHOR_RUNTIMES[0] if body.runtime == "any" else body.runtime
        pinned = None if body.worker_id is None else await pinnable(conn, user, access, body.worker_id)
        picked = await _picked(conn, user, access, body, pinned)
        repos = await _author_repos(conn, access, pinned)
        _fits(pinned, access, "author", [repos[0]["repo"]], runtime)
        placed = placement(body)
        asked = Asked("author", runtime, body.model, pinned, _picks(picked), **placed)
        chosen = await choose(conn, user, access, body.profile, asked)
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
            "pinned_worker_id": None if pinned is None else pinned.id,
            "requested_runtime": runtime,
            "runtime": runtime,
            "model": chosen.model,
            "mode": "headless",
            "approval": "auto",
            "timeout_s": body.timeout_h * 3600,
            "repos": repos,
            "request": body.request,
            **_listed(placed),
            **chosen.columns(),
        }
        try:
            run_id = await insert_run(conn, values)
        except IntegrityError as exc:
            if isinstance(exc.orig, psycopg.errors.CheckViolation):
                raise HTTPException(
                    422, "a repo name, or the number of repos, is not one a run can hold; nothing was dispatched"
                ) from None
            raise
        await record_pick(conn, run_id, picked)
        await notify_queued(conn, run_id)
        target = f"{project}/author run:{run_id}{_account_text(picked)}{chosen.audit_text()}"
        await audit_run(conn, user, access, audit.RUN_DISPATCH_AUTHOR, target)
        view = await run_view(conn, run_id)
    log.info(
        "author run dispatched",
        extra={
            "project": project,
            "run_id": run_id,
            "worker_id": None if pinned is None else pinned.id,
            "repos": len(repos),
            "plan_id": body.plan_id,
        },
    )
    return view
