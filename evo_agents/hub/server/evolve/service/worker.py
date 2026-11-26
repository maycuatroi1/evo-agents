"""What the worker of an evolve run reads and posts with the run's own key (``evolve.EVOLVE_KEY_HEADER``): the run it
holds (``held_evolve``), its inputs (``inputs_of``), the baseline arms it keeps (``kept_arm``), its candidates and the
Pareto front they make (``kept_candidate``), and its end (``reported``)."""

from __future__ import annotations

import hmac
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import curator, evolve, runs
from evo_agents.hub.server import system_curation
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound, Unprocessable
from evo_agents.hub.server.changes.repository.views import charter_of
from evo_agents.hub.server.evolve.models import (
    ArmIn,
    ArmOut,
    BudgetInput,
    CandidateIn,
    CandidateOut,
    EvolveInputs,
    FrontItem,
    ReportIn,
    TaskInput,
    key_digest,
)
from evo_agents.hub.server.evolve.repository import candidates, evolutions, tasks
from evo_agents.hub.server.evolve.service import report
from evo_agents.hub.server.evolve.service.runs import cached_arms, settings_of, task_inputs
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models import NOT_HELD
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.security import Principal


async def held_evolve(conn: AsyncConnection, user: Principal, run_id: int, key: str | None):
    """The row of curator_evolve_runs of evolve run ``run_id``, which the worker of ``user`` holds, locked; 404 for any
    other run, 409 for an evolution that waits for nothing of it, 403 without the run's own key."""
    worker_id = (await worker_of(conn, user))[0]
    run = await evolutions.held_run(conn, run_id)
    if run is None or run.worker_id != worker_id or run.state not in runs.HELD_STATES:
        raise NotFound(NOT_HELD.format(id=run_id))
    if run.kind != "evolve":
        raise NotFound(f"run {run_id} is not an evolve run: only an evolve run evolves")
    row = await evolutions.of_run(conn, run_id)
    if row is None or row.state != "running":
        raise Conflict(f"run {run_id} evolves nothing that waits for it")
    if not key or row.evolve_key is None or not hmac.compare_digest(key_digest(key), row.evolve_key):
        raise Forbidden(
            f"run {run_id} is an evolve run: its inputs and results take the run's own key "
            f"({evolve.EVOLVE_KEY_HEADER}), which its claim handed the daemon; the worker token alone reads nothing",
        )
    return row


# Its inputs


async def _branch(conn: AsyncConnection, row) -> str | None:
    """The branch a git evolution pushes its best candidate to: the planned change's of its proposal, else its own."""
    if row.target != "git":
        return None
    if row.proposal_id is not None:
        found = await tasks.change_branch(conn, row.proposal_id)
        if found:
            return found
    return f"curator/evolve-{row.id}"


async def _baseline(conn: AsyncConnection, row) -> dict:
    if row.target != "agent_config":
        return {}
    _, profile = await system_curation.evolve_target(conn, row.project_id, row.system_id, row.agent)
    return system_curation.agent_baseline(profile)


async def inputs_of(conn: AsyncConnection, row) -> EvolveInputs:
    project = await evolutions.project_name(conn, row.project_id)
    charter = await charter_of(conn, row.project_id)
    budget = {"iterations": 0, "max_candidates": 0, "max_cost_usd": None} | dict(row.budget or {})
    found = await task_inputs(conn, row.project_id, list(row.tasks or []))
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
            TaskInput.model_validate({key: value for key, value in task.items() if key != "repo"}) for task in found
        ],
        cached=await cached_arms(conn, row),
        budget=BudgetInput.model_validate(budget),
        runtime=row.runtime or curator.DEFAULT_RUNTIME,
        model=row.model,
        evolvable_paths=list(settings_of(charter).evolvable_paths),
    )


# Its baseline arms and candidates


def _task_of(row, kind: str, task_id: int) -> None:
    if not any(task.get("kind") == kind and task.get("id") == task_id for task in row.tasks or []):
        raise Unprocessable(f"task {evolve.task_ref(kind, task_id)} is no task of evolution #{row.id}")


async def kept_arm(conn: AsyncConnection, row, run_id: int, body: ArmIn) -> ArmOut:
    """Keep the result of a baseline arm of one task of evolution ``row`` by its ``evolve.arm_key``; one the cache
    holds stays as it is."""
    _task_of(row, body.task_kind, body.task_id)
    key = evolve.arm_key(body.task_kind, body.task_id, body.base_sha, body.baseline_digest, body.runtime, body.model)
    values = body.model_dump() | {"project_id": row.project_id, "key": key, "run_id": run_id}
    made = await candidates.cache_arm(conn, values)
    return ArmOut(key=key, cached=made is None)


async def _frontier(conn: AsyncConnection, row) -> list[dict]:
    """Recompute the Pareto front of evolution ``row`` over all its candidates, and keep it."""
    rows = await candidates.measures(conn, row.id)
    scores = {str(item.id): {k: float(v) for k, v in (item.scores or {}).items()} for item in rows}
    costs = {str(item.id): {"cost_usd": item.cost_usd, "turns": item.turns} for item in rows}
    survivors = evolve.prune_all(scores, costs)
    freq = evolve.front_freq(scores, survivors)
    frontier = [{"candidate_id": int(cand), "freq": freq[cand]} for cand in survivors]
    await candidates.mark_front(conn, row.id, [int(cand) for cand in survivors])
    await evolutions.set_frontier(conn, row.id, frontier)
    return frontier


async def _checked_candidate(conn: AsyncConnection, row, body: CandidateIn) -> str:
    try:
        digest = evolve.baseline_digest(body.components)
    except evolve.EvolveProblem as exc:
        raise Unprocessable(str(exc)) from None
    refs = {task.get("ref") or evolve.task_ref(task["kind"], task["id"]) for task in row.tasks or []}
    unknown = sorted(set(body.scores) - refs)
    if unknown:
        raise Unprocessable(f"{', '.join(unknown[:5])}: no task of evolution #{row.id}")
    if body.parents and await candidates.known_parents(conn, row.id, body.parents) != len(set(body.parents)):
        raise Unprocessable(f"a parent of the candidate is no candidate of evolution #{row.id}")
    return digest


async def kept_candidate(conn: AsyncConnection, row, body: CandidateIn) -> CandidateOut:
    """Keep a candidate of evolution ``row``, unique by the digest of its components, and recompute its Pareto
    front."""
    digest = await _checked_candidate(conn, row, body)
    values = body.model_dump() | {"project_id": row.project_id, "evolve_id": row.id, "digest": digest}
    values["traces"] = evolve.asi_of(body.traces, body.scores)
    measured = {key: values[key] for key in ("scores", "cost_usd", "turns", "traces", "iteration")}
    candidate_id = await candidates.kept(conn, values, measured)
    await evolutions.set_iterations(conn, row.id, max(row.iterations, body.iteration))
    frontier = await _frontier(conn, row)
    return CandidateOut(candidate_id=candidate_id, frontier=[FrontItem.model_validate(item) for item in frontier])


# Its end


async def reported(conn: AsyncConnection, user: Principal, row, run_id: int, body: ReportIn) -> tuple[str, Any]:
    """End evolution ``row`` as its run reports, with an event of the run that says how; (its project, its row now)."""
    text = await report.ended(conn, user, row, run_id, body)
    await write_event(conn, run_id, {"text": text, "evolve_id": row.id})
    row = await evolutions.one(conn, row.id)
    return await evolutions.project_name(conn, row.project_id), row
