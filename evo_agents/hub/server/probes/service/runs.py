"""The probe runs of the Curator on the hub: each active probe of a merged change measured once an ISO week on each
runtime and model of the charter (``evo_agents.hub.probes`` holds their shape, ``service.probes`` the probes
themselves, ``repository.runs`` the queries on curator_probe_runs).

The night shift (``curator_shift._fire``) queues a probe run last, inside its window, once the night has no plan to run
(``queue_probe``): for a charter with ``probes.budget_usd``, the pairs of an active probe of a merged change (with its
plan), or of a core plugin's rule (no change and no plan: ``source`` plugin), and a model of ``probes.models`` that this week (the ISO week of the night, its Monday in the charter's time zone)
has not measured yet, the one measured longest ago first (never measured before all), on a pair whose repos and runtime
the worker on duty takes. A run of kind probe on the change's plan, pinned to that worker, as the schedule's owner, on
the pair's runtime and model, with the night's caps of the next run cut to what the week's probes have left; the
schedule's caps (max_runs_per_night, the night's budget, the per-run cap) hold it as every run of the night. Once the
week's probes spent ``budget_usd`` (less than curator.MIN_RUN_USD left), each pair the week has not measured is recorded
``unmeasured`` with why. A probe run that ends without every session is queued again, PROBE_ATTEMPTS runs in all, then
fails with why (``probe_ended``).

The claim of a probe run tells its worker the probe (its repo, base commit, task and verify), the change (its repo,
merge commit, head and default branch), k, and hands it the run's own key (``probe_claim``), which the hub keeps as a
SHA-256: the worker posts each session's result, with where the arms ran (``arms_at``: tip, else change, which the row
keeps), or why neither the tip of the default branch less the change's diff nor the change's commits make the arms
(stale), with POST /v1/worker/runs/{id}/probe (``routes.runs``, ``report``) and that key alone
(``probes.PROBE_KEY_HEADER``). The run's events never hold the verify, the answer key, and the agent of a run reads no
probe run (``views.agent_reads``).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import date

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import curator, probes, runs
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.plans import service as plan_service
from evo_agents.hub.server.probes.models.runs import (
    PROBE_RUN,
    REQUEUED,
    UNMEASURED,
    ProbeReport,
    ProbeRequeue,
    ProbeRun,
    ProbeSession,
)
from evo_agents.hub.server.probes.repository import runs as repository
from evo_agents.hub.server.run_state import RunStep, notify_queued, write_event
from evo_agents.hub.server.runs.models import NOT_HELD
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.runs.service.dispatch import insert_run
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)


# Queueing


def _key_digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def probe_models(body: dict) -> list[tuple[str, str | None]]:
    """The runtimes and models of the charter's probes: ``probes.models``, else the builder's."""
    found = [
        (item.get("runtime") or curator.DEFAULT_RUNTIME, item.get("model"))
        for item in (body.get("probes") or {}).get("models") or []
        if isinstance(item, dict)
    ]
    builder = body.get("builder") or {}
    return found or [(builder.get("runtime") or curator.DEFAULT_RUNTIME, builder.get("model"))]


async def _pairs(conn: AsyncConnection, project_id: int, week: date, models: list[tuple[str, str | None]]) -> list:
    """(probe, change, runtime, model, row of the week or None) of each pair the week has yet to measure, the one
    measured longest ago first, then by probe and by the charter's order of models."""
    probes_found = await repository.measurable(conn, project_id)
    if not probes_found:
        return []
    rows = await repository.rows_of(conn, [row.id for row in probes_found])
    this_week = {(row.probe_id, row.runtime, row.model): row for row in rows if row.week == week}
    measured: dict[tuple, date] = {}
    for row in rows:
        if row.week < week and row.state == "done":
            key = (row.probe_id, row.runtime, row.model)
            measured[key] = max(measured.get(key, row.week), row.week)  # pyright: ignore[reportArgumentType]
    pairs = []
    for probe in probes_found:
        for order, (runtime, model) in enumerate(models):
            key = (probe.id, runtime, model)
            row = this_week.get(key)
            if row is not None and row.state != "queued":
                continue  # measured, held, stale, failed or unmeasured this week
            pairs.append((measured.get(key, date.min), probe.id, order, probe, runtime, model, row))
    pairs.sort(key=lambda item: item[:3])
    return [(probe, runtime, model, row) for *_, probe, runtime, model, row in pairs]


async def _unmeasured(conn: AsyncConnection, due, week: date, k: int, pairs: list, why: str) -> None:
    for probe, runtime, model, row in pairs:
        if row is not None:
            await repository.unmeasured_row(conn, row.id, why)
            continue
        values = {"project_id": due.project_id, "probe_id": probe.id, "change_id": probe.change_id, "week": week}
        values |= {"runtime": runtime, "model": model, "k": k, "state": "unmeasured", "reason": why}
        await repository.insert_unless_held(conn, values)
    log.info("curator probes unmeasured", extra={"project": due.project, "pairs": len(pairs), "week": str(week)})


def _repos(probe) -> list[dict]:
    repos = [{"repo": probe.repo, "branch": None}] if probe.repo != probe.change_repo else []
    return [*repos, {"repo": probe.change_repo, "branch": None}]


def probe_words(probe) -> str:
    """What a probe of ``probe`` measures, after "probe #<id> of": the Curator's change, or a plugin's rule."""
    if probe.change_id is None and probe.ref is not None:
        return f"plugin {probe.plugin or 'removed'} rule {probe.ref}"
    return f"the Curator's change #{probe.change_id}"


def _values(due, gate, probe, held, runtime: str, model: str | None, budget: dict) -> dict:
    words = f"{runtime}{f' {model}' if model else ''}"
    return {
        "kind": "probe",
        "project_id": due.project_id,
        "plan_id": probe.plan_id,  # None: a plugin's probe, which has no plan
        "title": f"Probe #{probe.id} of {probe_words(probe)} on {words}"[:200],
        "plan_revision": None if held is None else held.revision,
        "dispatched_by": due.owner_id,
        "dispatched_via": "schedule",
        **gate.duty.columns(),
        "requested_runtime": runtime,
        "runtime": runtime,
        "model": model,
        "mode": "headless",
        "approval": "auto",
        "timeout_s": budget["max_seconds"] + curator.BUDGET_GRACE_SECONDS,
        "max_attempts": 1,  # a probe run that ends short is queued again by its row, not the reaper
        "repos": _repos(probe),
        "schedule_id": due.id,
        "schedule_night": gate.night,
        "budget": budget,
    }


async def _queued(conn: AsyncConnection, due, gate, week: date, k: int, found, run_id: int, budget: dict) -> None:
    probe, runtime, model, row = found
    values = {"run_id": run_id, "state": "running", "k": k, "probe_key": None, "sessions": [], "reason": None}
    if row is None:
        values |= {"project_id": due.project_id, "probe_id": probe.id, "change_id": probe.change_id, "week": week}
        await repository.insert_row(conn, {**values, "runtime": runtime, "model": model})
    else:
        values |= {"base_sha": None, "harness_sha": None, "arms_at": None}
        await repository.update_row(conn, row.id, values)
    caps = f"cost cap {curator.money(budget['max_usd'])}, {budget['max_seconds'] // 60} minutes of agent time"
    whose = "the change's" if probe.change_id is not None else "the plugin's"
    text = (
        f"Queued by the night shift of project {due.project} as probe #{probe.id} of {probe_words(probe)} "
        f"for the week of {week.isoformat()}: {k} session(s) with {whose} rule on and {k} with "
        f"it off, on {runtime}{f' {model}' if model else ''}, each in a new directory at the probe's base commit: {caps}."
    )
    event = {"text": text, "change_id": probe.change_id, "probe_id": probe.id, **budget}
    if probe.change_id is None:
        event |= {"plugin": probe.plugin, "ref": probe.ref}
    await write_event(conn, run_id, event)
    await notify_queued(conn, run_id)
    await audit.record(
        conn,
        actor_id=due.owner_id,
        token_id=None,
        action=PROBE_RUN,
        target=f"{due.project}/{probe.plan_id or f'plugin:{probe.plugin}'} run:{run_id} probe:{probe.id} "
        f"week:{week.isoformat()}",
        project_id=due.project_id,
    )
    log.info("curator probe queued", extra={"project": due.project, "run_id": run_id, "probe_id": probe.id})


async def queue_probe(conn: AsyncConnection, due, gate) -> int | None:
    """Queue the next probe run of the project (see the module's docstring); its id, or None. Called under the
    schedule's row lock."""
    settings = due.body.get("probes") or {}
    cap = settings.get("budget_usd")
    if cap is None:
        return None
    week = probes.week_of(gate.night)
    k = int(settings.get("k") or probes.DEFAULT_K)
    pairs = await _pairs(conn, due.project_id, week, probe_models(due.body))
    if not pairs:
        return None
    spent = await repository.week_cost(conn, due.id, week)
    if cap - spent < curator.MIN_RUN_USD:
        await _unmeasured(
            conn, due, week, k, pairs, UNMEASURED.format(spent=curator.money(spent), cap=curator.money(cap))
        )
        return None
    left = min(gate.budget["max_usd"], cap - spent)
    for found in pairs:
        probe, runtime, model, _ = found
        if gate.duty.unfit(due.project, "probe", [item["repo"] for item in _repos(probe)], runtime):
            continue
        held = None
        if probe.plan_id is not None:  # a plugin's probe has no plan
            held = await plan_service.held(conn, gate.access, probe.plan_id)
            if held is None:
                continue
        budget = {**gate.budget, "max_usd": round(left, 4)}
        try:
            run_id = await insert_run(conn, _values(due, gate, probe, held, runtime, model, budget))
        except IntegrityError:  # another probe run of the project is active
            return None
        await _queued(conn, due, gate, week, k, found, run_id, budget)
        return run_id
    return None


# An admin's requeue


async def requeue(conn: AsyncConnection, probe_id: int, week: date) -> ProbeRequeue:
    """Put the rows of probe ``probe_id`` of the ISO week of Monday ``week`` that are stale, failed or unmeasured back
    to queued, their attempts at 0 and no reason, so that the next night measures them again (``_pairs`` takes a queued
    row); the sessions a row holds stay until its next run starts. Any other row is kept as it is."""
    rows = await repository.week_rows(conn, probe_id, week)
    again = [row.id for row in rows if row.state in REQUEUED]
    if again:
        await repository.requeue_rows(conn, again)
    found = [await repository.run_view(conn, row.id) for row in rows]
    return ProbeRequeue(
        probe_id=probe_id,
        week=week,
        requeued=[item for item in found if item.id in again],
        kept=[item for item in found if item.id not in again],
    )


# The claim of a probe run


async def probe_claim(conn: AsyncConnection, change, run_id: int) -> dict:
    """What the claim of probe run ``run_id`` of ``change`` adds for its worker: the probe, the change's commits, k and
    a new key of the run (``probe_key``), whose SHA-256 alone the hub keeps; nothing when the run probes nothing now.
    A plugin's probe has no change (``change`` None): its spec names the plugin, the rule's ``ref`` and the files of
    each arm (``arms``), which go in the probe's own repo."""
    row = await repository.row_of_run(conn, run_id, running=True)
    if row is None:
        return {}
    probe = await repository.probe_of(conn, row.probe_id)
    key = secrets.token_urlsafe(32)
    await repository.update_row(conn, row.id, {"probe_key": _key_digest(key)})
    spec = {
        "probe_run_id": row.id,
        "probe_id": probe.id,
        "repo": probe.repo,
        "base_sha": probe.base_sha,
        "made_at": probe.created_at,
        "task": probe.task,
        "verify": probe.verify,
        "k": row.k,
        "week": row.week,
        "change_repo": probe.repo if change is None else change.repo,
        "merge_sha": None if change is None else change.merge_sha,
    }
    if probe.arms is not None:
        plugin = await repository.plugin_name(conn, probe.plugin_id)
        spec |= {"plugin": plugin, "ref": probe.ref, "arms": probe.arms}
    elif change is None:
        return {}  # a probe of a change, on a run that names none: it probes nothing now
    return {"probe": spec, "probe_key": key}


async def run_prompt(conn: AsyncConnection, view) -> str:
    """The prompt of probe run ``view``, as its worker builds it for each session (``probes.probe_prompt``)."""
    probe = await repository.task_of_run(conn, view.id)
    if probe is None:
        return "This probe run probes nothing: end it."
    return probes.probe_prompt(probe.task, folder=probe.repo)


# The probe run's side


async def _held_probe(conn: AsyncConnection, user: Principal, run_id: int, key: str | None):
    """The row of curator_probe_runs of probe run ``run_id``, which the worker of ``user`` holds, locked; 404 for any
    other run, 409 for a probe that waits for nothing of it, 403 without the run's own key."""
    worker_id = (await worker_of(conn, user))[0]
    run = await repository.held_run(conn, run_id)
    if run is None or run.worker_id != worker_id or run.state not in runs.HELD_STATES:
        raise NotFound(NOT_HELD.format(id=run_id))
    if run.kind != "probe":
        raise NotFound(f"run {run_id} is not a probe run: only a probe run reports a probe")
    row = await repository.row_of_run(conn, run_id)
    if row is None or row.state != "running":
        raise Conflict(f"run {run_id} probes nothing that waits for it")
    if not key or row.probe_key is None or not hmac.compare_digest(_key_digest(key), row.probe_key):
        raise Forbidden(
            f"run {run_id} is a probe run: its results take the run's own key ({probes.PROBE_KEY_HEADER}), which its "
            "claim handed the daemon; the worker token alone writes no result"
        )
    return row


async def _stale(conn: AsyncConnection, row, body: ProbeReport, run_id: int) -> None:
    values = {"state": "stale", "reason": body.stale}
    values |= {key: getattr(body, key) for key in ("base_sha", "harness_sha") if getattr(body, key)}
    await repository.update_row(conn, row.id, values)
    await write_event(conn, run_id, {"text": f"The probe is stale this week: {body.stale}.", "stale": True})


async def _session(conn: AsyncConnection, row, body: ProbeReport, session: ProbeSession, run_id: int) -> None:
    if session.index > row.k:
        raise Conflict(f"run {run_id} takes {row.k} session(s) of each arm, not session {session.index}")
    if any(item.get("arm") == session.arm and item.get("index") == session.index for item in row.sessions or []):
        raise Conflict(f"run {run_id} reported session {session.index} of arm {session.arm} already")
    sessions = [*(row.sessions or []), session.model_dump()]
    values = {"sessions": sessions, "base_sha": row.base_sha or body.base_sha}
    values |= {"harness_sha": row.harness_sha or body.harness_sha}
    if row.change_id is not None:  # a worker that names no arms_at ran the arms at the tip
        values["arms_at"] = row.arms_at or body.arms_at or probes.ARMS_AT_TIP
    if len(sessions) >= 2 * row.k:
        values |= {"state": "done", "reason": None}
    await repository.update_row(conn, row.id, values)
    word = "passed" if session.passed else "failed"
    text = f"Session {session.index} of arm {session.arm} of the probe {word} its verify."
    await write_event(
        conn, run_id, {"text": text, "arm": session.arm, "index": session.index, "passed": session.passed}
    )


async def report(engine: AsyncEngine, user: Principal, run_id: int, body: ProbeReport, key: str | None) -> ProbeRun:
    """One session's result of the probe run the worker of ``user`` holds, or why the probe is stale; with the run's
    own ``key``, in a transaction of its own. The row as it is then, logged once that commits."""
    async with UnitOfWork(engine) as conn:
        row = await _held_probe(conn, user, run_id, key)
        if body.session is None:
            await _stale(conn, row, body, run_id)
        else:
            await _session(conn, row, body, body.session, run_id)
        found = await repository.run_view(conn, row.id)
    log.info("probe reported", extra={"run_id": run_id, "probe_run_id": found.id, "state": found.state})
    return found


async def probe_ended(conn: AsyncConnection, found: RunStep, old: str, new: str, *, reason: str) -> None:
    """What the end of probe run ``found`` does to its row: one that ended without every session is queued again,
    PROBE_ATTEMPTS runs in all, then fails with why; a failure sends the owner the notice run_failed."""
    if new not in runs.TERMINAL_STATES or found.project_id is None:
        return
    from evo_agents.hub.server.notifications.service.notifier import NOTIFIER

    why = found.error or reason
    if new == "failed":
        await NOTIFIER.notify(
            conn,
            user_id=found.dispatcher_id,
            kind="notice",
            notice_kind="run_failed",
            project_id=found.project_id,
            run_id=found.run_id,
            title=f"Probe run #{found.run_id} of {found.plan_id or 'a plugin rule'} failed",
            body=why,
            details={"run_kind": "probe", "plan_id": found.plan_id, "error": why},
            link=NOTIFIER.run_link(found.project, found.run_id),
        )
    row = await repository.row_of_run(conn, found.run_id)
    if row is None or row.state != "running":
        return
    attempts = row.attempts + 1
    last = attempts >= probes.PROBE_ATTEMPTS
    text = f"probe run #{found.run_id} ended {new} with {len(row.sessions or [])} of {2 * row.k} sessions ({why})"
    if last:
        text = f"{attempts} probe runs ended without every session, the last {new}: {why}"
    values = {"state": "failed" if last else "queued", "attempts": attempts, "reason": text[: probes.MAX_REASON_CHARS]}
    await repository.update_row(conn, row.id, values)
