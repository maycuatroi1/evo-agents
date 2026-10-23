"""The probe runs of the Curator on the hub: each active probe of a merged change measured once an ISO week on each
runtime and model of the charter (``evo_agents.hub.probes`` holds their shape, ``evo_agents.hub.server.probes`` the
probes themselves).

The night shift (``curator._fire``) queues a probe run last, inside its window, once the night has no plan to run
(``queue_probe``): for a charter with ``probes.budget_usd``, the pairs of an active probe of a merged change (with its
plan) and a model of ``probes.models`` that this week (the ISO week of the night, its Monday in the charter's time zone)
has not measured yet, the one measured longest ago first (never measured before all), on a pair whose repos and runtime
the worker on duty takes. A run of kind probe on the change's plan, pinned to that worker, as the schedule's owner, on
the pair's runtime and model, with the night's caps of the next run cut to what the week's probes have left; the
schedule's caps (max_runs_per_night, the night's budget, the per-run cap) hold it as every run of the night. Once the
week's probes spent ``budget_usd`` (less than curator.MIN_RUN_USD left), each pair the week has not measured is recorded
``unmeasured`` with why. A probe run that ends without every session is queued again, PROBE_ATTEMPTS runs in all, then
fails with why (``probe_ended``).

The claim of a probe run tells its worker the probe (its repo, base commit, task and verify), the change (its repo,
merge commit, head and default branch), k, and hands it the run's own key (``probe_claim``), which the hub keeps as a
SHA-256: the worker posts each session's result, or why the change's diff no longer comes off the default branch's tip
(stale), with POST /v1/worker/runs/{id}/probe and that key alone (``probes.PROBE_KEY_HEADER``). The run's events never
hold the verify, the answer key, and the agent of a run reads no probe run (``views.agent_reads``).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import curator, probes, runs, tables
from evo_agents.hub.server import audit
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.night_cost import usage_cost
from evo_agents.hub.server.run_state import RunStep, notify_queued, write_event
from evo_agents.hub.server.runs.models import NOT_HELD, OBJECT_NAME, RunId
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.runs.service.dispatch import insert_run, unfit
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

PROBE_RUN = "curator.probe_run"  # the night shift queued a probe run: "<project>/<plan> run:<id> probe:<id> week:<day>"
UNMEASURED = "the week's probes spent probes.budget_usd ({spent} of {cap}) before this probe ran on this model"

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409)}

ProbeKey = Annotated[
    str | None,
    Header(
        alias=probes.PROBE_KEY_HEADER,
        max_length=200,
        description="the probe run's own key, which its claim handed the daemon; the worker token alone is refused",
    ),
]
Reason = Annotated[str, Field(min_length=1, max_length=probes.MAX_REASON_CHARS)]


# Models


class ProbeSession(BaseModel):
    """One session of a probe run: its arm, which of the arm's k it is, whether the probe's verify passed on its work,
    and what the agent took, on the runtime and the model it actually ran."""

    model_config = ConfigDict(extra="forbid")

    arm: Literal[probes.ARMS] = Field(description="on: the change's rule in place; off: the change's diff taken off")
    index: int = Field(ge=1, le=probes.MAX_K, description="which of the arm's k sessions, from 1")
    passed: bool = Field(description="the probe's verify exited 0 on the session's work")
    exit_code: int | None = Field(None, description="the verify's; null when it did not run")
    cost_usd: float | None = Field(None, ge=0, description="the agent's session; null when its runtime names none")
    turns: int | None = Field(None, ge=0)
    duration_ms: int = Field(ge=0, description="the agent's time")
    runtime: Literal[runs.RUNTIMES]
    model: str | None = Field(None, max_length=200, description="the model the session ran on, as its runtime said it")
    agent_error: str | None = Field(None, max_length=probes.MAX_REASON_CHARS, description="why its turn ended short")
    session_id: str | None = Field(None, max_length=200)


class ProbeReport(BaseModel):
    """A session's result with the commits the arms run from; or why the change's diff no longer comes off the tip of
    the default branch (``stale``)."""

    model_config = ConfigDict(extra="forbid")

    base_sha: str | None = Field(None, pattern=OBJECT_NAME, description="the task's base commit the sessions start at")
    harness_sha: str | None = Field(
        None, pattern=OBJECT_NAME, description="the tip of the default branch of the change's repo arm on runs with"
    )
    session: ProbeSession | None = None
    stale: Reason | None = Field(None, description="why arm off cannot be made: the probe is stale for the week")

    @model_validator(mode="after")
    def _one(self):
        if (self.session is None) == (self.stale is None):
            raise ValueError("a probe report is one session with its base_sha and harness_sha, or stale with why")
        if self.session is not None and (self.base_sha is None or self.harness_sha is None):
            raise ValueError("a probe session names the base_sha and harness_sha its arms ran from")
        return self


class ProbeRun(BaseModel):
    id: int
    probe_id: int
    change_id: int
    week: date = Field(description="the Monday of the ISO week, in the charter's time zone")
    runtime: str
    model: str | None
    k: int = Field(description="the sessions of each arm")
    run_id: int | None
    state: Literal[probes.RUN_STATES]
    attempts: int
    base_sha: str | None
    harness_sha: str | None
    sessions: list[dict] = Field(description="ProbeSession, in the order they were reported")
    reason: str | None
    created_at: datetime
    updated_at: datetime


# Queueing


def _key_digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _models(body: dict) -> list[tuple[str, str | None]]:
    """The runtimes and models of the charter's probes: ``probes.models``, else the builder's."""
    found = [
        (item.get("runtime") or curator.DEFAULT_RUNTIME, item.get("model"))
        for item in (body.get("probes") or {}).get("models") or []
        if isinstance(item, dict)
    ]
    builder = body.get("builder") or {}
    return found or [(builder.get("runtime") or curator.DEFAULT_RUNTIME, builder.get("model"))]


async def week_cost(conn: AsyncConnection, schedule_id: int, week: date) -> float:
    """What the probe runs schedule ``schedule_id`` queued for the nights of the ISO week of ``week`` cost."""
    r = tables.runs
    of_week = (
        r.c.schedule_id == schedule_id,
        r.c.kind == "probe",
        r.c.schedule_night.between(week, week + timedelta(days=6)),
    )
    found = await conn.execute(select(func.coalesce(func.sum(usage_cost(r.c.usage)), 0)).where(*of_week))
    return float(found.scalar_one() or 0)


async def _pairs(conn: AsyncConnection, project_id: int, week: date, models: list[tuple[str, str | None]]) -> list:
    """(probe, change, runtime, model, row of the week or None) of each pair the week has yet to measure, the one
    measured longest ago first, then by probe and by the charter's order of models."""
    pr, c, pw = tables.curator_probes, tables.curator_changes, tables.curator_probe_runs
    found = await conn.execute(
        select(pr, c.c.plan_id.label("plan_id"), c.c.repo.label("change_repo"), c.c.branch.label("change_branch"))
        .join(c, c.c.id == pr.c.change_id)
        .where(
            pr.c.project_id == project_id,
            pr.c.state == "active",
            c.c.state == "merged",
            c.c.plan_id.is_not(None),
        )
        .order_by(pr.c.id)
    )
    probes_found = found.all()
    if not probes_found:
        return []
    ids = [row.id for row in probes_found]
    rows = (await conn.execute(select(pw).where(pw.c.probe_id.in_(ids)))).all()
    this_week = {(row.probe_id, row.runtime, row.model): row for row in rows if row.week == week}
    measured: dict[tuple, date] = {}
    for row in rows:
        if row.week < week and row.state == "done":
            key = (row.probe_id, row.runtime, row.model)
            measured[key] = max(measured.get(key, row.week), row.week)
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
    pw = tables.curator_probe_runs
    for probe, runtime, model, row in pairs:
        if row is not None:
            values = {"state": "unmeasured", "reason": why, "updated_at": func.now()}
            await conn.execute(update(pw).values(**values).where(pw.c.id == row.id, pw.c.state == "queued"))
            continue
        values = {"project_id": due.project_id, "probe_id": probe.id, "change_id": probe.change_id, "week": week}
        values |= {"runtime": runtime, "model": model, "k": k, "state": "unmeasured", "reason": why}
        await conn.execute(pg_insert(pw).values(**values).on_conflict_do_nothing())
    log.info("curator probes unmeasured", extra={"project": due.project, "pairs": len(pairs), "week": str(week)})


def _repos(probe) -> list[dict]:
    repos = [{"repo": probe.repo, "branch": None}] if probe.repo != probe.change_repo else []
    return [*repos, {"repo": probe.change_repo, "branch": None}]


def _values(due, gate, probe, held, runtime: str, model: str | None, budget: dict) -> dict:
    words = f"{runtime}{f' {model}' if model else ''}"
    return {
        "kind": "probe",
        "project_id": due.project_id,
        "plan_id": probe.plan_id,
        "title": f"Probe #{probe.id} of the Curator's change #{probe.change_id} on {words}"[:200],
        "plan_revision": held.revision,
        "dispatched_by": due.owner_id,
        "dispatched_via": "schedule",
        "pinned_worker_id": gate.worker.id,
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
    pw = tables.curator_probe_runs
    values = {"run_id": run_id, "state": "running", "k": k, "probe_key": None, "sessions": [], "reason": None}
    if row is None:
        values |= {"project_id": due.project_id, "probe_id": probe.id, "change_id": probe.change_id, "week": week}
        await conn.execute(pg_insert(pw).values(**values, runtime=runtime, model=model))
    else:
        values |= {"base_sha": None, "harness_sha": None, "updated_at": func.now()}
        await conn.execute(update(pw).values(**values).where(pw.c.id == row.id))
    caps = f"cost cap {curator.money(budget['max_usd'])}, {budget['max_seconds'] // 60} minutes of agent time"
    text = (
        f"Queued by the night shift of project {due.project} as probe #{probe.id} of the Curator's change "
        f"#{probe.change_id} for the week of {week.isoformat()}: {k} session(s) with the change's rule on and {k} with "
        f"it off, on {runtime}{f' {model}' if model else ''}, each in a new directory at the probe's base commit: {caps}."
    )
    await write_event(conn, run_id, {"text": text, "change_id": probe.change_id, "probe_id": probe.id, **budget})
    await notify_queued(conn, run_id)
    await audit.record(
        conn,
        actor_id=due.owner_id,
        token_id=None,
        action=PROBE_RUN,
        target=f"{due.project}/{probe.plan_id} run:{run_id} probe:{probe.id} week:{week.isoformat()}",
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
    pairs = await _pairs(conn, due.project_id, week, _models(due.body))
    if not pairs:
        return None
    spent = await week_cost(conn, due.id, week)
    if cap - spent < curator.MIN_RUN_USD:
        await _unmeasured(
            conn, due, week, k, pairs, UNMEASURED.format(spent=curator.money(spent), cap=curator.money(cap))
        )
        return None
    left = min(gate.budget["max_usd"], cap - spent)
    for found in pairs:
        probe, runtime, model, _ = found
        if unfit(gate.worker, due.project, "probe", [item["repo"] for item in _repos(probe)], runtime):
            continue
        held = await plan_routes._held(conn, gate.access, probe.plan_id)
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


# The claim of a probe run


async def probe_claim(conn: AsyncConnection, change, run_id: int) -> dict:
    """What the claim of probe run ``run_id`` of ``change`` adds for its worker: the probe, the change's commits, k and
    a new key of the run (``probe_key``), whose SHA-256 alone the hub keeps; nothing when the run probes nothing now."""
    pw, pr = tables.curator_probe_runs, tables.curator_probes
    row = (
        await conn.execute(select(pw).where(pw.c.run_id == run_id, pw.c.state == "running").with_for_update())
    ).one_or_none()
    if row is None:
        return {}
    probe = (await conn.execute(select(pr).where(pr.c.id == row.probe_id))).one()
    key = secrets.token_urlsafe(32)
    await conn.execute(update(pw).values(probe_key=_key_digest(key), updated_at=func.now()).where(pw.c.id == row.id))
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
        "change_repo": change.repo,
        "merge_sha": change.merge_sha,
    }
    return {"probe": spec, "probe_key": key}


async def run_prompt(conn: AsyncConnection, view) -> str:
    """The prompt of probe run ``view``, as its worker builds it for each session (``probes.probe_prompt``)."""
    pw, pr = tables.curator_probe_runs, tables.curator_probes
    probe = (
        await conn.execute(
            select(pr.c.task, pr.c.repo).join(pw, pw.c.probe_id == pr.c.id).where(pw.c.run_id == view.id)
        )
    ).one_or_none()
    if probe is None:
        return "This probe run probes nothing: end it."
    return probes.probe_prompt(probe.task, folder=probe.repo)


# The probe run's side


async def _held_probe(conn: AsyncConnection, user: Principal, run_id: int, key: str | None):
    """The row of curator_probe_runs of probe run ``run_id``, which the worker of ``user`` holds, locked; 404 for any
    other run, 409 for a probe that waits for nothing of it, 403 without the run's own key."""
    worker_id = (await worker_of(conn, user))[0]
    r, pw = tables.runs, tables.curator_probe_runs
    run = (
        await conn.execute(select(r.c.state, r.c.worker_id, r.c.kind).where(r.c.id == run_id).with_for_update())
    ).one_or_none()
    if run is None or run.worker_id != worker_id or run.state not in runs.HELD_STATES:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    if run.kind != "probe":
        raise HTTPException(404, f"run {run_id} is not a probe run: only a probe run reports a probe")
    row = (await conn.execute(select(pw).where(pw.c.run_id == run_id).with_for_update())).one_or_none()
    if row is None or row.state != "running":
        raise HTTPException(409, f"run {run_id} probes nothing that waits for it")
    if not key or row.probe_key is None or not hmac.compare_digest(_key_digest(key), row.probe_key):
        raise HTTPException(
            403,
            f"run {run_id} is a probe run: its results take the run's own key ({probes.PROBE_KEY_HEADER}), which its "
            "claim handed the daemon; the worker token alone writes no result",
        )
    return row


async def _stale(conn: AsyncConnection, row, body: ProbeReport, run_id: int) -> None:
    pw = tables.curator_probe_runs
    values = {"state": "stale", "reason": body.stale, "updated_at": func.now()}
    values |= {key: getattr(body, key) for key in ("base_sha", "harness_sha") if getattr(body, key)}
    await conn.execute(update(pw).values(**values).where(pw.c.id == row.id))
    await write_event(conn, run_id, {"text": f"The probe is stale this week: {body.stale}.", "stale": True})


async def _session(conn: AsyncConnection, row, body: ProbeReport, run_id: int) -> None:
    pw, session = tables.curator_probe_runs, body.session
    if session.index > row.k:
        raise HTTPException(409, f"run {run_id} takes {row.k} session(s) of each arm, not session {session.index}")
    if any(item.get("arm") == session.arm and item.get("index") == session.index for item in row.sessions or []):
        raise HTTPException(409, f"run {run_id} reported session {session.index} of arm {session.arm} already")
    sessions = [*(row.sessions or []), session.model_dump()]
    values = {"sessions": sessions, "base_sha": row.base_sha or body.base_sha}
    values |= {"harness_sha": row.harness_sha or body.harness_sha, "updated_at": func.now()}
    if len(sessions) >= 2 * row.k:
        values |= {"state": "done", "reason": None}
    await conn.execute(update(pw).values(**values).where(pw.c.id == row.id))
    word = "passed" if session.passed else "failed"
    text = f"Session {session.index} of arm {session.arm} of the probe {word} its verify."
    await write_event(
        conn, run_id, {"text": text, "arm": session.arm, "index": session.index, "passed": session.passed}
    )


async def _view(conn: AsyncConnection, row_id: int) -> ProbeRun:
    pw = tables.curator_probe_runs
    row = (await conn.execute(select(pw).where(pw.c.id == row_id))).one()
    return ProbeRun(**{key: getattr(row, key) for key in ProbeRun.model_fields})


@worker_router.post("/runs/{run_id}/probe", response_model=ProbeRun, responses=REFUSALS)
async def report_probe(
    request: Request, run_id: RunId, body: ProbeReport, user: CurrentUser, probe_key: ProbeKey = None
) -> ProbeRun:
    """One session's result of the probe run this worker holds, or why the probe is stale; with the run's key."""
    async with request.app.state.engine.begin() as conn:
        row = await _held_probe(conn, user, run_id, probe_key)
        if body.stale is not None:
            await _stale(conn, row, body, run_id)
        else:
            await _session(conn, row, body, run_id)
        found = await _view(conn, row.id)
    log.info("probe reported", extra={"run_id": run_id, "probe_run_id": row.id, "state": found.state})
    return found


async def probe_ended(conn: AsyncConnection, found: RunStep, old: str, new: str, *, reason: str) -> None:
    """What the end of probe run ``found`` does to its row: one that ended without every session is queued again,
    PROBE_ATTEMPTS runs in all, then fails with why; a failure sends the owner the notice run_failed."""
    if new not in runs.TERMINAL_STATES or found.project_id is None:
        return
    from evo_agents.hub.server.notifications import notify, run_link

    why = found.error or reason
    if new == "failed":
        await notify(
            conn,
            user_id=found.dispatcher_id,
            kind="notice",
            notice_kind="run_failed",
            project_id=found.project_id,
            run_id=found.run_id,
            title=f"Probe run #{found.run_id} of {found.plan_id} failed",
            body=why,
            details={"run_kind": "probe", "plan_id": found.plan_id, "error": why},
            link=run_link(found.project, found.run_id),
        )
    pw = tables.curator_probe_runs
    row = (await conn.execute(select(pw).where(pw.c.run_id == found.run_id).with_for_update())).one_or_none()
    if row is None or row.state != "running":
        return
    attempts = row.attempts + 1
    last = attempts >= probes.PROBE_ATTEMPTS
    text = f"probe run #{found.run_id} ended {new} with {len(row.sessions or [])} of {2 * row.k} sessions ({why})"
    if last:
        text = f"{attempts} probe runs ended without every session, the last {new}: {why}"
    values = {"state": "failed" if last else "queued", "attempts": attempts, "reason": text[: probes.MAX_REASON_CHARS]}
    await conn.execute(update(pw).values(**values, updated_at=func.now()).where(pw.c.id == row.id))
