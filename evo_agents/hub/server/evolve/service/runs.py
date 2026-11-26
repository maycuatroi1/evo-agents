"""The evolve runs of the Curator on the hub: an evolution of a harness in a repo (target ``git``) or of an agent's
configuration in an agent system (target ``agent_config``), its tasks, its run, its claim and its end
(``evo_agents.hub.evolve`` holds the model, ``evo_agents.hub.server.evolve.routes`` the routes).

An evolution is a row of curator_evolve_runs. When it starts (``start``), the hub picks its tasks once: light replay
tasks of the ``select`` split and active probes of the paths it evolves (``evolve.light_tasks``); a task of the holdout
split is never read. Its run (``queue_run``) is a run of kind evolve, as the night shift's other runs: as the schedule's
owner, placed on the worker or the pool on duty, with the caps of the night and the cost cap of the charter's
``evolve.budget``, at most one active run of the kind a project (runs_active_evolve_key). While the night shift's gate
is shut the evolution stays queued, with why, and the night shift queues its run later (``queue_evolve``); the night
shift never starts an evolution of its own.

The claim of the run hands its worker the evolution's id and the run's own key (``evolve_claim``), whose SHA-256 alone
the hub keeps in curator_evolve_runs.evolve_key; the worker reads its inputs and posts its arms, candidates and report
with that key alone (``evolve.EVOLVE_KEY_HEADER``). A run that ends before its report fails its evolution, with why
(``evolve_ended``).
"""

from __future__ import annotations

import logging
import secrets

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import curator, evolve, runs
from evo_agents.hub.server import audit
from evo_agents.hub.server.curator.models.settings import EvolveSettings
from evo_agents.hub.server.evolve.models import EVOLVE_RUN, OPEN_STATES, key_digest, reason_text
from evo_agents.hub.server.evolve.repository import candidates, evolutions, tasks
from evo_agents.hub.server.evolve.repository.evolutions import set_row
from evo_agents.hub.server.run_state import RunStep, notify_queued, write_event
from evo_agents.hub.server.runs.service.dispatch import insert_run

log = logging.getLogger(__name__)


def settings_of(charter: dict) -> EvolveSettings:
    """The charter's ``evolve`` block, its defaults for a charter written before it had one."""
    found = charter.get("evolve") if isinstance(charter.get("evolve"), dict) else {}
    return EvolveSettings.model_validate(found)


# The tasks


def _probe_paths(arms) -> list[str]:
    """The files the arms of a probe place, which say which paths it measures."""
    found: list[str] = []
    for side in (arms or {}).values() if isinstance(arms, dict) else ():
        found += [path for path in side or {} if isinstance(path, str)]
    return list(dict.fromkeys(found))


async def pick_tasks(conn: AsyncConnection, project_id: int, repo: str | None, paths: list[str], settings) -> list:
    """The light tasks of an evolution of ``paths`` in ``repo`` (None: every repo, an agent's configuration): the
    select split of the project's replay tasks alone, read so, and its active probes (``evolve.light_tasks``)."""
    rows = await tasks.select_replays(conn, project_id)
    replays = [{**row._asdict(), "commits": list(row.commits or [])} for row in rows]
    rows = await tasks.active_probes(conn, project_id)
    found = [{**row._asdict(), "paths": _probe_paths(row.arms)} for row in rows]
    picked = evolve.light_tasks(
        replays, found, repo=repo, paths=paths, limit=settings.tasks, probe_limit=settings.probes
    )
    return [{**task, "ref": evolve.task_ref(task["kind"], task["id"])} for task in picked]


async def task_inputs(conn: AsyncConnection, project_id: int, picked: list[dict]) -> list[dict]:
    """The tasks of an evolution as its trusted daemon gets them: each with its prompt, base commit, paths and verify,
    read again now; a replay task that left the select split, or a probe no longer active, is left out."""
    ids = {kind: [task["id"] for task in picked if task.get("kind") == kind] for kind in evolve.TASK_KINDS}
    replays = await tasks.replays_of(conn, project_id, ids["replay"])
    probes = await tasks.probes_of(conn, project_id, ids["probe"])
    found = []
    for task in picked:
        ref = evolve.task_ref(task["kind"], task["id"])
        if task["kind"] == "replay" and task["id"] in replays:
            row = replays[task["id"]]
            prompt = "\n\n".join(part for part in (row.title, row.what) if part)
            found.append(_task(task, ref, row.base_sha, prompt, [], row.verify, row.repo))
        elif task["kind"] == "probe" and task["id"] in probes:
            row = probes[task["id"]]
            found.append(_task(task, ref, row.base_sha, row.task, _probe_paths(row.arms), row.verify, row.repo))
    return found


def _task(task: dict, ref: str, base_sha, prompt: str, paths: list[str], verify: str | None, repo) -> dict:
    return {
        "kind": task["kind"],
        "id": task["id"],
        "ref": ref,
        "repo": repo,
        "base_sha": base_sha,
        "prompt": prompt or "",
        "paths": paths,
        "verify": [verify] if verify else [],
    }


async def cached_arms(conn: AsyncConnection, row) -> dict:
    """arm_key -> result of each baseline arm the project's cache holds for a task of evolution ``row``, on its
    runtime and model: the worker reads the one its own baseline's key names, and runs the rest."""
    pairs = {(task.get("kind"), task.get("id")) for task in row.tasks or []}
    if not pairs:
        return {}
    rows = await candidates.cached_arms(conn, row, [task_id for _, task_id in pairs])
    return {item.key: item.result for item in rows if (item.task_kind, item.task_id) in pairs}


# The run


def _values(due, gate, row, runtime: str, model: str | None, budget: dict) -> dict:
    what = row.repo if row.target == evolve.TARGETS[0] else f"agent {row.agent}"
    return {
        "kind": "evolve",
        "project_id": due.project_id,
        "plan_id": None,
        "title": f"Evolution #{row.id} of {what}"[:200],
        "dispatched_by": due.owner_id,
        "dispatched_via": "schedule",
        **gate.duty.columns(),
        "requested_runtime": runtime,
        "runtime": runtime,
        "model": model,
        "mode": "headless",
        "approval": "auto",
        "timeout_s": budget["max_seconds"] + curator.BUDGET_GRACE_SECONDS,
        "max_attempts": 1,  # an evolution that ends short fails with why: its owner starts another
        "schedule_id": due.id,
        "schedule_night": gate.night,
        "budget": budget,
        **({"repos": [{"repo": row.repo, "branch": None}]} if row.repo else {}),  # agent_config works in no repo
    }


def _budget(gate, row) -> dict:
    budget = dict(gate.budget)
    cap = (row.budget or {}).get("max_cost_usd")
    if isinstance(cap, (int, float)) and cap < budget["max_usd"]:
        budget["max_usd"] = float(cap)
    return budget


async def queue_run(conn: AsyncConnection, due, gate, row, *, asked_by: str | None = None) -> int | None:
    """Queue the run of queued evolution ``row`` as the night shift's other runs; its id, or None with why kept as
    the evolution's reason (no worker on duty fits it, or another evolve run of the project is active)."""
    runtime, model = row.runtime or curator.DEFAULT_RUNTIME, row.model
    problems = gate.duty.unfit(due.project, "evolve", [row.repo] if row.repo else [], runtime)
    if problems:
        await set_row(conn, row.id, reason=reason_text("; ".join(problems)))
        return None
    budget = _budget(gate, row)
    try:
        run_id = await insert_run(conn, _values(due, gate, row, runtime, model, budget))
    except IntegrityError as exc:
        if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) != "runs_active_evolve_key":
            raise
        await set_row(conn, row.id, reason="another evolve run of the project is active: it is queued once that ends")
        return None
    await set_row(conn, row.id, run_id=run_id, state="running", evolve_key=None, reason=None)
    caps = f"cost cap {curator.money(budget['max_usd'])}, {budget['max_seconds'] // 60} minutes of agent time"
    who = f"{asked_by} asked for it" if asked_by else "Queued by the night shift"
    text = (
        f"{who}: evolution #{row.id} of project {due.project}, target {row.target}, {len(row.tasks or [])} task(s) "
        f"of the select split and probes, on {runtime}{f' {model}' if model else ''}: {caps}."
    )
    await write_event(conn, run_id, {"text": text, "evolve_id": row.id, **budget})
    await notify_queued(conn, run_id)
    await audit.record(
        conn,
        actor_id=due.owner_id,
        token_id=None,
        action=EVOLVE_RUN,
        target=f"{due.project} run:{run_id} evolve:{row.id}",
        project_id=due.project_id,
    )
    log.info("evolve run queued", extra={"project": due.project, "run_id": run_id, "evolve_id": row.id})
    return run_id


async def queue_evolve(conn: AsyncConnection, due, gate) -> int | None:
    """The night shift's turn: the run of the oldest evolution an owner started that still waits for one, when the
    charter's ``evolve.enabled``; None when there is none or it waits."""
    if not settings_of(due.body).enabled:
        return None
    row = await evolutions.oldest_queued(conn, due.project_id)
    if row is None:
        return None
    return await queue_run(conn, due, gate, row)


async def evolve_claim(conn: AsyncConnection, run_id: int) -> dict:
    """What the claim of evolve run ``run_id`` adds for its worker: the evolution and a new key of the run
    (``evolve_key``), whose SHA-256 alone the hub keeps; nothing when the run evolves nothing now."""
    row = await evolutions.running_of_run(conn, run_id)
    if row is None:
        return {}
    key = secrets.token_urlsafe(32)
    await set_row(conn, row.id, evolve_key=key_digest(key))
    return {"evolve": {"evolve_id": row.id, "target": row.target}, "evolve_key": key}


async def run_prompt(conn: AsyncConnection, view) -> str:
    """The prompt of evolve run ``view``: its worker builds the prompt of each task and of the proposer itself."""
    row = await evolutions.target_of_run(conn, view.id)
    if row is None:
        return "This evolve run evolves nothing: end it."
    return f"Evolution #{row.id} of target {row.target}: the worker runs its loop and builds each prompt."


async def evolve_ended(conn: AsyncConnection, found: RunStep, new: str, *, reason: str) -> None:
    """What the end of evolve run ``found`` does: an evolution it left open fails, with why."""
    if new not in runs.TERMINAL_STATES:
        return
    row = await evolutions.state_of_run(conn, found.run_id)
    if row is None or row.state not in OPEN_STATES:
        return
    why = found.error or reason
    await set_row(
        conn, row.id, state="failed", evolve_key=None, reason=reason_text(f"run #{found.run_id} ended {new}: {why}")
    )
