"""The evolve routes of the Curator on the hub: an owner starts an evolution and reads it, and the worker of its run
reads its inputs and posts its baseline arms, its candidates and its end (``evo_agents.hub.server.evolve_runs`` holds
its tasks and its run, ``evo_agents.hub.server.evolve_report`` its end, ``evo_agents.hub.evolve`` the model).

A member with the writer role starts an evolution (POST /v1/projects/{project}/curator/evolve) when the charter's
``evolve.enabled``: of target git, harness files of a repo that ``evolve.evolvable_paths`` names; of target
agent_config, an agent that an optimizes edge of an agent system reaches. Its run is queued at once, at any hour, when
the night shift's gate is open to a run a member asks for; otherwise (paused, a spent night, no worker on duty) the
evolution waits, with why, and the night shift queues its run later.

The worker of the run reads and writes with the run's own key alone (``evolve.EVOLVE_KEY_HEADER``), which the claim
handed it: GET /v1/worker/runs/{id}/evolve gives the tasks with their verify, which only the trusted daemon reads, and
never a task of the holdout split; POST .../evolve/arm keeps the result of a baseline arm by ``evolve.arm_key``, read
again by the next evolution instead of run again; POST .../evolve/candidate keeps a candidate, unique by its digest,
and recomputes the Pareto front of the evolution (``evolve.prune_all``, ``evolve.front_freq``); POST .../evolve/report
ends it.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import curator, evolve, runs, tables
from evo_agents.hub.server import audit, evolve_report, evolve_runs, system_curation
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.changes.repository.views import charter_of
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.evolve_models import (
    ArmIn,
    ArmOut,
    BudgetInput,
    CandidateIn,
    CandidateOut,
    CandidateView,
    EvolveInputs,
    EvolveList,
    EvolveStart,
    EvolveTaskView,
    EvolveView,
    FrontItem,
    ReportIn,
    TaskInput,
)
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.plans import service as plan_service
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.protection import need_reader
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models import MAX_ID, NOT_HELD, RunId
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.runs.service.dispatch import check_dispatcher, check_seat
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

EVOLVE_START = "curator.evolve_start"  # a member started an evolution: "<project> evolve:<id> target:<target>"
MAX_LISTED = 100

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}

EvolveKey = Annotated[
    str | None,
    Header(
        alias=evolve.EVOLVE_KEY_HEADER,
        max_length=200,
        description="the evolve run's own key, which its claim handed the daemon; the worker token alone is refused",
    ),
]
Sink = Annotated[str | None, Header(alias=plan_models.SINK_HEADER)]
EvolveId = Annotated[int, Path(ge=1, le=MAX_ID)]


# Reading an evolution


async def candidates_of(conn: AsyncConnection, evolve_id: int, *, components: bool) -> list[CandidateView]:
    ec = tables.curator_evolve_candidates
    rows = (await conn.execute(select(ec).where(ec.c.evolve_id == evolve_id).order_by(ec.c.id))).all()
    return [
        CandidateView(
            id=row.id,
            parents=list(row.parents or []),
            origin=row.origin,
            iteration=row.iteration,
            digest=row.digest,
            scores=dict(row.scores or {}),
            cost_usd=row.cost_usd,
            turns=row.turns,
            on_front=row.on_front,
            components=dict(row.components) if components else None,
            asi=evolve.asi_of(row.traces),
            created_at=row.created_at,
        )
        for row in rows
    ]


def view_of(project: str, row, candidates: list[CandidateView] | None = None) -> EvolveView:
    """An evolution as its owner reads it: its tasks without their verify."""
    tasks = [
        {"kind": task["kind"], "id": task["id"], "ref": task.get("ref") or evolve.task_ref(task["kind"], task["id"])}
        | {"repo": task.get("repo")}
        for task in row.tasks or []
        if isinstance(task, dict) and task.get("kind") in evolve.TASK_KINDS
    ]
    return EvolveView(
        id=row.id,
        project=project,
        run_id=row.run_id,
        target=row.target,
        repo=row.repo,
        paths=list(row.paths or []),
        system_id=row.system_id,
        agent=row.agent,
        state=row.state,
        runtime=row.runtime,
        model=row.model,
        budget=dict(row.budget or {}),
        tasks=[EvolveTaskView.model_validate(task) for task in tasks],
        iterations=row.iterations,
        frontier=[FrontItem.model_validate(item) for item in row.frontier or []],
        best_candidate_id=row.best_candidate_id,
        proposal_id=row.proposal_id,
        change_id=row.change_id,
        reason=row.reason,
        created_at=row.created_at,
        updated_at=row.updated_at,
        candidates=candidates,
    )


async def evolutions_of(
    conn: AsyncConnection, access: ProjectAccess, sink: str | None, *, state: str | None = None, limit: int = 20
) -> list:
    """The project's evolutions the caller may read through ``sink``, the newest first."""
    e = tables.curator_evolve_runs
    query = select(e).where(e.c.project_id == access.project_id)
    if state is not None:
        query = query.where(e.c.state == state)
    rows = (await conn.execute(query.order_by(e.c.id.desc()).limit(limit))).all()
    return [row for row in rows if access.visible(row.label, plan_service.read_sink(access, sink) or "")]


async def evolution_of(conn: AsyncConnection, access: ProjectAccess, evolve_id: int, sink: str | None):
    """Evolution ``evolve_id`` of the project; 404 when there is none the caller may read."""
    e = tables.curator_evolve_runs
    query = select(e).where(e.c.id == evolve_id, e.c.project_id == access.project_id)
    row = (await conn.execute(query)).one_or_none()
    if row is None or not access.visible(row.label, plan_service.read_sink(access, sink) or ""):
        raise HTTPException(404, f"project {access.name} has no evolution #{evolve_id}")
    return row


# Starting an evolution


async def _checked_git(conn: AsyncConnection, access: ProjectAccess, body: EvolveStart, globs: list[str]) -> None:
    pr = tables.project_repos
    query = select(pr.c.name).where(pr.c.project_id == access.project_id, pr.c.name == body.repo)
    if await conn.scalar(query) is None:
        raise HTTPException(422, f"project {access.name} has no repo {body.repo}")
    outside = [path for path in body.paths if not evolve.evolvable(body.repo or "", path, globs)]
    if outside:
        raise HTTPException(
            422,
            f"{', '.join(outside[:5])} of {body.repo}: no harness file the charter's evolve.evolvable_paths names; "
            "nothing was started",
        )


async def _checked_proposal(conn: AsyncConnection, access: ProjectAccess, proposal_id: int | None) -> None:
    if proposal_id is None:
        return
    p = tables.proposals
    query = select(p.c.id).where(p.c.id == proposal_id, p.c.project_id == access.project_id)
    if await conn.scalar(query) is None:
        raise HTTPException(404, f"project {access.name} has no proposal #{proposal_id}")


async def _gate(conn: AsyncConnection, project_id: int):
    """(due, word, gate) of the project's night shift for an evolution its owner asks for now: at any hour, beside the
    schedule's other runs, as an asked-for KG optimize run (``kg_optimize._gate``)."""
    from evo_agents.hub.server.curator_shift import due_schedules, gate_of  # it imports the routes of the night shift

    due = (await conn.execute(due_schedules(None).where(tables.schedules.c.project_id == project_id))).one_or_none()
    if due is None:
        return None, "none", None
    word, gate = await gate_of(conn, due, any_hour=True, beside=True, on_demand=True)
    return due, word, gate


async def start(conn: AsyncConnection, user: Principal, project: str, body: EvolveStart) -> int:
    """Start an evolution (see the module's docstring); its id. Its run is queued now when the gate is open."""
    from evo_agents.hub.server.build_now import GATE_REFUSALS  # it imports the routes of the night shift
    from evo_agents.hub.server.night_duty import on_duty

    access = await project_access(conn, user, project)
    check_dispatcher(access)
    await check_seat(conn, user)
    charter = await charter_of(conn, access.project_id)
    settings = evolve_runs.settings_of(charter)
    if not settings.enabled:
        raise HTTPException(409, f"the charter of project {access.name} does not enable evolve: nothing was started")
    if body.target == "git":
        await _checked_git(conn, access, body, settings.evolvable_paths)
    elif body.system_id is not None and body.agent is not None:  # EvolveStart holds both for target agent_config
        await system_curation.evolve_target(conn, access.project_id, body.system_id, body.agent)
    await _checked_proposal(conn, access, body.proposal_id)
    tasks = await evolve_runs.pick_tasks(conn, access.project_id, body.repo, body.paths, settings)
    e = tables.curator_evolve_runs
    values = body.model_dump(exclude={"target"}) | {"target": body.target, "project_id": access.project_id}
    values |= {"label": access.push_label(), "budget": settings.budget.model_dump(), "tasks": tasks}
    values |= {"runtime": (charter.get("builder") or {}).get("runtime") or curator.DEFAULT_RUNTIME}
    values |= {"model": (charter.get("builder") or {}).get("model"), "created_by": user.user_id}
    evolve_id: int = (await conn.execute(pg_insert(e).values(**values).returning(e.c.id))).scalar_one()
    target = f"{access.name} evolve:{evolve_id} target:{body.target}"
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=EVOLVE_START,
        target=target,
        project_id=access.project_id,
    )
    due, word, gate = await _gate(conn, access.project_id)
    if gate is None:
        why = GATE_REFUSALS.get(word, word)
        if word == "pool":
            why = (await on_duty(conn, due))[1] or why
        await evolve_runs.set_row(
            conn, evolve_id, reason=evolve_runs.reason_text(f"it waits for the night shift: {why}")
        )
    else:
        row = (await conn.execute(select(e).where(e.c.id == evolve_id).with_for_update())).one()
        await evolve_runs.queue_run(conn, due, gate, row, asked_by=user.login)
    log.info("evolution started", extra={"project": access.name, "evolve_id": evolve_id, "target": body.target})
    return evolve_id


@router.post("/{project}/curator/evolve", status_code=201, response_model=EvolveView, responses=REFUSALS)
async def start_evolution(request: Request, project: ProjectName, body: EvolveStart, user: CurrentUser) -> EvolveView:
    """Start an evolution of a harness's files in a repo (git) or of an agent's configuration (agent_config)."""
    async with request.app.state.engine.begin() as conn:
        evolve_id = await start(conn, user, project, body)
        e = tables.curator_evolve_runs
        row = (await conn.execute(select(e).where(e.c.id == evolve_id))).one()
        return view_of(project, row, [])


@router.get("/{project}/curator/evolve", response_model=EvolveList, responses=REFUSALS)
async def list_evolutions(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    state: Annotated[str | None, Query(pattern="^(queued|running|done|failed)$")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LISTED)] = 20,
    sink: Sink = None,
) -> EvolveList:
    """The project's evolutions, the newest first, each with its state, tasks and front."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        rows = await evolutions_of(conn, access, sink, state=state, limit=limit)
    return EvolveList(project=access.name, evolutions=[view_of(access.name, row) for row in rows])


@router.get("/{project}/curator/evolve/{evolve_id}", response_model=EvolveView, responses=REFUSALS)
async def show_evolution(
    request: Request, project: ProjectName, evolve_id: EvolveId, user: CurrentUser, sink: Sink = None
) -> EvolveView:
    """One evolution, with each candidate (its components, scores, cost and ASI of each task) and its front."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        row = await evolution_of(conn, access, evolve_id, sink)
        candidates = await candidates_of(conn, row.id, components=True)
    return view_of(access.name, row, candidates)


# The worker of the run


async def _held_evolve(conn: AsyncConnection, user: Principal, run_id: int, key: str | None):
    """The row of curator_evolve_runs of evolve run ``run_id``, which the worker of ``user`` holds, locked; 404 for any
    other run, 409 for an evolution that waits for nothing of it, 403 without the run's own key."""
    worker_id = (await worker_of(conn, user))[0]
    r, e = tables.runs, tables.curator_evolve_runs
    query = select(r.c.state, r.c.worker_id, r.c.kind).where(r.c.id == run_id).with_for_update()
    run = (await conn.execute(query)).one_or_none()
    if run is None or run.worker_id != worker_id or run.state not in runs.HELD_STATES:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    if run.kind != "evolve":
        raise HTTPException(404, f"run {run_id} is not an evolve run: only an evolve run evolves")
    row = (await conn.execute(select(e).where(e.c.run_id == run_id).with_for_update())).one_or_none()
    if row is None or row.state != "running":
        raise HTTPException(409, f"run {run_id} evolves nothing that waits for it")
    if not key or row.evolve_key is None or not hmac.compare_digest(evolve_runs.key_digest(key), row.evolve_key):
        raise HTTPException(
            403,
            f"run {run_id} is an evolve run: its inputs and results take the run's own key "
            f"({evolve.EVOLVE_KEY_HEADER}), which its claim handed the daemon; the worker token alone reads nothing",
        )
    return row


async def _branch(conn: AsyncConnection, row) -> str | None:
    """The branch a git evolution pushes its best candidate to: the planned change's of its proposal, else its own."""
    if row.target != "git":
        return None
    if row.proposal_id is not None:
        c = tables.curator_changes
        found = await conn.scalar(select(c.c.branch).where(c.c.proposal_id == row.proposal_id))
        if found:
            return found
    return f"curator/evolve-{row.id}"


async def _baseline(conn: AsyncConnection, row) -> dict:
    if row.target != "agent_config":
        return {}
    _, profile = await system_curation.evolve_target(conn, row.project_id, row.system_id, row.agent)
    return system_curation.agent_baseline(profile)


async def inputs_of(conn: AsyncConnection, row) -> EvolveInputs:
    p = tables.projects
    project = (await conn.execute(select(p.c.name).where(p.c.id == row.project_id))).scalar_one()
    charter = await charter_of(conn, row.project_id)
    budget = {"iterations": 0, "max_candidates": 0, "max_cost_usd": None} | dict(row.budget or {})
    tasks = await evolve_runs.task_inputs(conn, row.project_id, list(row.tasks or []))
    return EvolveInputs(
        evolve_id=row.id,
        project=project,
        target=row.target,
        repo=row.repo,
        base_sha=None,
        branch=await _branch(conn, row),
        paths=list(row.paths or []),
        system_id=row.system_id,
        agent=row.agent,
        baseline=await _baseline(conn, row),
        tasks=[
            TaskInput.model_validate({key: value for key, value in task.items() if key != "repo"}) for task in tasks
        ],
        cached=await evolve_runs.cached_arms(conn, row),
        budget=BudgetInput.model_validate(budget),
        runtime=row.runtime or curator.DEFAULT_RUNTIME,
        model=row.model,
        evolvable_paths=list(evolve_runs.settings_of(charter).evolvable_paths),
    )


@worker_router.get("/runs/{run_id}/evolve", response_model=EvolveInputs, responses=REFUSALS)
async def evolve_inputs(request: Request, run_id: RunId, user: CurrentUser, evolve_key: EvolveKey = None):
    """What the held evolve run evolves: its baseline, its light tasks with their verify, the cached baseline arms."""
    async with request.app.state.engine.begin() as conn:
        row = await _held_evolve(conn, user, run_id, evolve_key)
        return await inputs_of(conn, row)


def _task_of(row, kind: str, task_id: int) -> None:
    if not any(task.get("kind") == kind and task.get("id") == task_id for task in row.tasks or []):
        raise HTTPException(422, f"task {evolve.task_ref(kind, task_id)} is no task of evolution #{row.id}")


@worker_router.post("/runs/{run_id}/evolve/arm", response_model=ArmOut, responses=REFUSALS)
async def evolve_arm(
    request: Request, run_id: RunId, body: ArmIn, user: CurrentUser, evolve_key: EvolveKey = None
) -> ArmOut:
    """Keep the result of a baseline arm of one task by its ``evolve.arm_key``; one the cache holds stays as it is."""
    async with request.app.state.engine.begin() as conn:
        row = await _held_evolve(conn, user, run_id, evolve_key)
        _task_of(row, body.task_kind, body.task_id)
        key = evolve.arm_key(
            body.task_kind, body.task_id, body.base_sha, body.baseline_digest, body.runtime, body.model
        )
        ac = tables.curator_evolve_arm_cache
        values = body.model_dump() | {"project_id": row.project_id, "key": key, "run_id": run_id}
        query = pg_insert(ac).values(**values).on_conflict_do_nothing(index_elements=["project_id", "key"])
        made = await conn.scalar(query.returning(ac.c.key))
    return ArmOut(key=key, cached=made is None)


async def _frontier(conn: AsyncConnection, row) -> list[dict]:
    """Recompute the Pareto front of evolution ``row`` over all its candidates, and keep it."""
    ec, e = tables.curator_evolve_candidates, tables.curator_evolve_runs
    query = select(ec.c.id, ec.c.scores, ec.c.cost_usd, ec.c.turns).where(ec.c.evolve_id == row.id).order_by(ec.c.id)
    rows = (await conn.execute(query)).all()
    scores = {str(item.id): {k: float(v) for k, v in (item.scores or {}).items()} for item in rows}
    costs = {str(item.id): {"cost_usd": item.cost_usd, "turns": item.turns} for item in rows}
    survivors = evolve.prune_all(scores, costs)
    freq = evolve.front_freq(scores, survivors)
    frontier = [{"candidate_id": int(cand), "freq": freq[cand]} for cand in survivors]
    on = [int(cand) for cand in survivors]
    await conn.execute(
        update(ec).values(on_front=ec.c.id.in_(on), updated_at=func.now()).where(ec.c.evolve_id == row.id)
    )
    await conn.execute(update(e).values(frontier=frontier, updated_at=func.now()).where(e.c.id == row.id))
    return frontier


async def _checked_candidate(conn: AsyncConnection, row, body: CandidateIn) -> str:
    try:
        digest = evolve.baseline_digest(body.components)
    except evolve.EvolveProblem as exc:
        raise HTTPException(422, str(exc)) from None
    refs = {task.get("ref") or evolve.task_ref(task["kind"], task["id"]) for task in row.tasks or []}
    unknown = sorted(set(body.scores) - refs)
    if unknown:
        raise HTTPException(422, f"{', '.join(unknown[:5])}: no task of evolution #{row.id}")
    ec = tables.curator_evolve_candidates
    query = select(func.count()).where(ec.c.evolve_id == row.id, ec.c.id.in_(body.parents))
    if body.parents and await conn.scalar(query) != len(set(body.parents)):
        raise HTTPException(422, f"a parent of the candidate is no candidate of evolution #{row.id}")
    return digest


@worker_router.post("/runs/{run_id}/evolve/candidate", response_model=CandidateOut, responses=REFUSALS)
async def evolve_candidate(
    request: Request, run_id: RunId, body: CandidateIn, user: CurrentUser, evolve_key: EvolveKey = None
) -> CandidateOut:
    """Keep a candidate, unique by the digest of its components, and recompute the evolution's Pareto front."""
    async with request.app.state.engine.begin() as conn:
        row = await _held_evolve(conn, user, run_id, evolve_key)
        digest = await _checked_candidate(conn, row, body)
        ec, e = tables.curator_evolve_candidates, tables.curator_evolve_runs
        values = body.model_dump() | {"project_id": row.project_id, "evolve_id": row.id, "digest": digest}
        values["traces"] = evolve.asi_of(body.traces, body.scores)
        measured = {key: values[key] for key in ("scores", "cost_usd", "turns", "traces", "iteration")}
        query = pg_insert(ec).values(**values)
        query = query.on_conflict_do_update(
            constraint="curator_evolve_candidates_evolve_id_digest_key", set_={**measured, "updated_at": func.now()}
        )
        candidate_id = await conn.scalar(query.returning(ec.c.id))
        iterations = max(row.iterations, body.iteration)
        await conn.execute(update(e).values(iterations=iterations).where(e.c.id == row.id))
        frontier = await _frontier(conn, row)
    return CandidateOut(candidate_id=candidate_id, frontier=[FrontItem.model_validate(item) for item in frontier])


@worker_router.post("/runs/{run_id}/evolve/report", response_model=EvolveView, responses=REFUSALS)
async def evolve_report_route(
    request: Request, run_id: RunId, body: ReportIn, user: CurrentUser, evolve_key: EvolveKey = None
) -> EvolveView:
    """End the evolution: of target git, its branch goes on as its proposal's change; of target agent_config, its best
    candidate is pinned as the agent's next version when it holds on the holdout split; why, either way."""
    async with request.app.state.engine.begin() as conn:
        row = await _held_evolve(conn, user, run_id, evolve_key)
        text = await evolve_report.ended(conn, user, row, run_id, body)
        await write_event(conn, run_id, {"text": text, "evolve_id": row.id})
        e = tables.curator_evolve_runs
        row = (await conn.execute(select(e).where(e.c.id == row.id))).one()
        p = tables.projects
        project = (await conn.execute(select(p.c.name).where(p.c.id == row.project_id))).scalar_one()
    log.info("evolution reported", extra={"run_id": run_id, "evolve_id": row.id, "state": row.state})
    return view_of(project, row)


# The MCP tool


def _candidate_line(item: CandidateView) -> str:
    cost = curator.money(item.cost_usd) if item.cost_usd is not None else "cost -"
    scores = ", ".join(f"{ref} {value:g}" for ref, value in sorted(item.scores.items())) or "no score"
    parents = f" of {', '.join(f'#{p}' for p in item.parents)}" if item.parents else ""
    front = " (on the front)" if item.on_front else ""
    return (
        f"#{item.id} {item.origin}{parents}, round {item.iteration}{front}: {scores}; {cost}, {item.turns or 0} turns"
    )


def _evolution_line(view: EvolveView) -> str:
    what = f"{view.repo} {', '.join(view.paths)}" if view.target == "git" else f"agent {view.agent}"
    best = f", best #{view.best_candidate_id}" if view.best_candidate_id else ""
    why = f": {view.reason}" if view.reason else ""
    return f"#{view.id} {view.target} of {what} ({view.state}), {view.iterations} round(s){best}{why}"


async def mcp_answer(request: Request, project: str, user: Principal, arguments: dict, *, sink: str | None):
    """(text, data) of the MCP tool curator_evolve: each evolution, or one with its select tasks, candidates, scores
    and front. Its tasks are of the select split and probes alone, and never with their verify; no candidate's
    components are in it."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        if "evolve_id" in arguments:
            row = await evolution_of(conn, access, arguments["evolve_id"], sink)
            return _one_answer(view_of(access.name, row, await candidates_of(conn, row.id, components=False)))
        rows = await evolutions_of(conn, access, sink, state=arguments.get("state"), limit=arguments.get("limit", 20))
        return _list_answer(project, [view_of(access.name, row) for row in rows])


def _list_answer(project: str, views: list[EvolveView]) -> tuple[str, dict]:
    lines = [f"The evolutions of project {project}: {len(views)}."] + [_evolution_line(view) for view in views]
    data = {"project": project, "evolutions": [view.model_dump(mode="json") for view in views]}
    return "\n".join(lines), {**data, "summary": f"{len(views)} evolutions"}


def _one_answer(view: EvolveView) -> tuple[str, dict]:
    lines = [f"Evolution {_evolution_line(view)}."]
    lines.append("Tasks (select split and probes): " + (", ".join(task.ref for task in view.tasks) or "none"))
    lines += [_candidate_line(item) for item in view.candidates or []]
    front = ", ".join(f"#{item.candidate_id} leads {item.freq}" for item in view.frontier) or "none yet"
    lines.append(f"Front: {front}.")
    data = view.model_dump(mode="json", exclude={"candidates": {"__all__": {"components"}}})
    return "\n".join(lines), {**data, "summary": f"{len(view.candidates or [])} candidates"}
