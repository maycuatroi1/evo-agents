"""Runs on the hub: the steps of a plan that may be dispatched, dispatching them, the worker's side of the queue
(claim, heartbeat, state) and the owner's cancel, approve and rerun. ``docs/workers.md`` is the protocol, and
``run_state`` moves the runs and records each move in the plan.

GET /v1/projects/{p}/plans/{plan}/ready-steps (reader) lists every step of the plan, ready or not, with why it is not
(``runs.unready_reason``, or the active run it has). POST /v1/projects/{p}/runs (writer) queues one run per step
named: every step must be ready and without an active run (409 otherwise, and nothing is queued), and a worker named
must be one of the caller's own (403 for any other id, a hub admin's included), live and serving the project (409).
Each run is notified on RUNS_CHANNEL and audited (run.dispatch).

A worker claims with POST /v1/worker/claim, which waits up to CLAIM_WAIT_SECONDS: the api process LISTENs on
RUNS_CHANNEL on one connection of its own (``RunWakeups``, opened by the first claim) and looks again every
CLAIM_POLL_SECONDS besides, so a lost notification delays a claim but never loses it. A claim takes the oldest queued
run, ``FOR UPDATE SKIP LOCKED``, that its owner dispatched, of a project the worker serves and on which the owner
still holds writer, asking for a runtime the worker reported (``any`` takes the first of runs.RUNTIMES it has), of a
repo it has a checkout of, pinned to no other worker, while the worker is neither draining nor revoked and holds
fewer runs than its slots. A worker has one claim waiting at a time: a newer claim answers the older one with no run.
The claimed run is leased for LEASE_SECONDS and comes with its prompt (``runs.build_prompt`` over the plan revision
it was dispatched from).

POST /v1/worker/heartbeat records the machine (runtimes, checkouts keyed ``<project>/<repo>``, free slots), extends
the lease of every run the worker names and still holds, and answers with control: per run, whether to cancel (asked
by the owner, or a run the worker no longer holds), takeover, handback, terminal_open and how many inbox messages
wait; for the worker, whether to drain. Takeover, handback and the terminal are always false until the routes that
ask for them exist. POST /v1/worker/runs/{id}/state reports a move of a run the worker holds (404 otherwise), checked
against ``runs.TRANSITIONS`` with the worker as actor (409 otherwise): ``done`` only for approval auto with every
verify command exited 0, ``review`` only for approval review. Reporting the state the run is in already changes
nothing but the session id, commit, diffstat, verify results and usage given, so a resend is safe.

The owner of a run is the member who dispatched it. Cancel moves a queued run or one in review to ``cancelled`` and
asks the worker holding a held run to stop it (the next heartbeat says cancel); approve moves a run in review to
``done``; rerun queues the step again, at the plan's current revision, after a run that ended. Each one is audited
(run.cancel, run.approve, run.rerun); another member gets 403, someone without a grant on the project 404.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Header, HTTPException, Path, Request
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from evo_agents.hub import runs
from evo_agents.hub.access import has_role
from evo_agents.hub.db import CONNECT_TIMEOUT
from evo_agents.hub.plans import PlanProblem, step_index, step_key
from evo_agents.hub.server import audit
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.run_state import (
    LEASE,
    RUNS_CHANNEL,
    evidence,
    move_run,
    notify_queued,
    run_step,
)
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

MAX_ID = 2**63 - 1  # bigint
MAX_DISPATCH_STEPS = 50
CLAIM_POLL_SECONDS = 5.0  # a waiting claim looks again this often, notification or not
LISTEN_BACKOFF = (1.0, 60.0)  # seconds between attempts to LISTEN again after the connection failed
MAX_HEARTBEAT_RUNS = 64
MAX_REPORT_BYTES = 64 * 1024  # runtimes and checkouts of a heartbeat, or the usage of a state report, as JSON
LINE = r"^[^\x00-\x1f\x7f]+$"
OBJECT_NAME = r"^([0-9a-f]{40}|[0-9a-f]{64})$"
CHECKOUT_KEY = r"^[a-z0-9][a-z0-9-]{0,99}/[^\x00-\x1f\x7f/][^\x00-\x1f\x7f]{0,199}$"  # <project>/<repo>
STEP_KEY_CHARS = 200
APPLICATION_NAME = "evo-agents-hub-runs"
REQUESTED_RUNTIMES = ("any", *runs.RUNTIMES)
WRITER_ROLES = [role for role in ("reader", "writer", "admin") if has_role(role, "writer")]
NOT_HELD = "this worker does not hold run {id}: it may have been lost, cancelled or taken by another attempt"

router = APIRouter(prefix="/v1/projects", tags=["runs"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})

RunId = Annotated[int, Path(ge=1, le=MAX_ID)]
RuntimeName = Literal[runs.RUNTIMES]  # an alias: a model with a field named runs cannot say runs.RUNTIMES
REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 409: {"model": ErrorBody}}


# Models


class ActiveRun(BaseModel):
    id: int
    state: Literal[runs.ACTIVE_STATES]
    dispatched_by: str


class StepReadiness(BaseModel):
    key: str = Field(description="the step's id, else its order, else its position, as plans name steps")
    title: str | None
    repo: str | None = Field(description="the repo its run would check out, as the plan's repos name it")
    status: str | None = Field(description="as the plan holds it; pending when the step has none")
    ready: bool = Field(description="whether a dispatch of it now would be taken")
    reason: str | None = Field(description="why it is not ready, in words to show next to it")
    active_run: ActiveRun | None


class ReadySteps(BaseModel):
    project: str
    plan_id: str
    revision: int
    steps: list[StepReadiness] = Field(description="every step of the plan, in plan order")


class Dispatch(BaseModel):
    plan_id: str = Field(pattern=plan_routes.PLAN_ID)
    steps: list[int | Annotated[str, Field(min_length=1, max_length=STEP_KEY_CHARS)]] = Field(
        min_length=1, max_length=MAX_DISPATCH_STEPS, description="step ids (or orders), one run each"
    )
    runtime: Literal[REQUESTED_RUNTIMES] = Field("any", description="any: the claiming worker picks one it has")
    mode: Literal[runs.MODES] = "headless"
    worker_id: int | None = Field(None, ge=1, le=MAX_ID, description="pin the runs to this worker of yours")
    approval: Literal[runs.APPROVALS] = Field("review", description="auto: verified runs mark the step done")
    timeout_min: int = Field(60, ge=5, le=240)


class Run(BaseModel):
    id: int
    project: str
    plan_id: str
    step_key: str
    plan_revision: int = Field(description="the plan revision the run was dispatched from")
    dispatched_by: str = Field(description="the login of the member who dispatched it, its owner")
    worker_id: int | None = Field(description="the worker that claimed it")
    worker: str | None = Field(description="that worker's name")
    pinned_worker_id: int | None
    requested_runtime: Literal[REQUESTED_RUNTIMES]
    runtime: Literal[REQUESTED_RUNTIMES] = Field(description="any until a worker claims the run")
    mode: Literal[runs.MODES]
    approval: Literal[runs.APPROVALS]
    timeout_min: int
    attempt: int
    max_attempts: int
    parent_run_id: int | None = Field(description="the run this one retries or reruns")
    state: Literal[runs.RUN_STATES]
    lease_expires_at: datetime | None
    session_id: str | None
    repo: str
    branch: str | None
    commit_sha: str | None
    diffstat: dict | None
    verify: list | None
    evidence: str | None
    usage: dict | None
    error: str | None
    cancel_requested_at: datetime | None
    queued_at: datetime
    leased_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None


class ClaimRequest(BaseModel):
    wait_s: float = Field(
        runs.CLAIM_WAIT_SECONDS, ge=0, le=runs.CLAIM_WAIT_SECONDS, description="how long to wait for a run"
    )


class RunSpec(BaseModel):
    id: int
    project: str
    plan_id: str
    step_key: str
    title: str | None
    plan_revision: int
    attempt: int
    max_attempts: int
    parent_run_id: int | None
    runtime: Literal[runs.RUNTIMES]
    mode: Literal[runs.MODES]
    approval: Literal[runs.APPROVALS]
    timeout_min: int
    repo: str
    branch: str | None
    lease_expires_at: datetime
    prompt: str


class Claim(BaseModel):
    run: RunSpec | None = Field(description="null when the wait ended without a run; claim again")


def _json_bytes(value) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())


class RuntimeReport(BaseModel):
    """One runtime as the daemon found it on the machine."""

    available: bool = Field(description="whether runs may use it; false with a reason when it is installed but not")
    version: str | None = Field(None, min_length=1, max_length=100, pattern=LINE)
    reason: str | None = Field(None, min_length=1, max_length=500, pattern=LINE, description="why it is unavailable")


class CheckoutReport(BaseModel):
    """One checkout of a project's repo on the machine, which the daemon makes worktrees from."""

    path: str = Field(min_length=1, max_length=1024, pattern=LINE)
    branch: str | None = Field(None, min_length=1, max_length=255, pattern=LINE, description="checked out there")


class HeartbeatRequest(BaseModel):
    runtimes: dict[RuntimeName, RuntimeReport] = Field(
        default_factory=dict,
        description='the runtimes on the machine: {"claude-code": {"available": true, "version": "2.1.289"}}',
    )
    checkouts: dict[Annotated[str, Field(pattern=CHECKOUT_KEY)], CheckoutReport] = Field(
        default_factory=dict,
        max_length=500,
        description='keyed <project>/<repo>: {"evo-agents/evo-agents": {"path": "/src/evo-agents", "branch": "main"}}',
    )
    free_slots: int = Field(ge=0, le=8)
    runs: list[Annotated[int, Field(ge=1, le=MAX_ID)]] = Field(
        default_factory=list, max_length=MAX_HEARTBEAT_RUNS, description="the runs the worker holds"
    )
    agent_version: str | None = Field(None, min_length=1, max_length=100, pattern=LINE)

    def stored(self) -> tuple[dict, dict]:
        """runtimes and checkouts as workers keeps them and GET /v1/workers/{id} shows them: every key present."""
        return (
            {name: report.model_dump() for name, report in self.runtimes.items()},
            {key: report.model_dump() for key, report in self.checkouts.items()},
        )

    @model_validator(mode="after")
    def _bounded(self):
        runtimes, checkouts = self.stored()
        if _json_bytes(runtimes) + _json_bytes(checkouts) > MAX_REPORT_BYTES:
            raise ValueError(f"runtimes and checkouts take at most {MAX_REPORT_BYTES} bytes of JSON")
        return self


def available(report) -> bool:
    """Whether a stored runtime may take runs: an object with available true (or without the key, as a version
    alone), true, or a version string."""
    if isinstance(report, dict):
        return report.get("available", True) is True
    return report is True or (isinstance(report, str) and bool(report))


class RunControl(BaseModel):
    id: int
    held: bool = Field(description="false for a run the worker no longer holds; stop it")
    state: Literal[runs.RUN_STATES] | None = Field(description="null for a run that is not this worker's")
    lease_expires_at: datetime | None
    cancel: bool
    takeover: bool = False
    handback: bool = False
    terminal_open: bool = False
    inbox: int = Field(0, description="messages from the owner waiting for the agent")


class HeartbeatAnswer(BaseModel):
    drain: bool = Field(description="claim no new run; finish the ones held")
    runs: list[RunControl]


class VerifyResult(BaseModel):
    command: str = Field(min_length=1, max_length=2000)
    exit_code: int
    duration_ms: int | None = Field(None, ge=0)


class Diffstat(BaseModel):
    files: int = Field(ge=0)
    insertions: int = Field(ge=0)
    deletions: int = Field(ge=0)


class StateReport(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    state: Literal[runs.RUN_STATES] = Field(description="the state the run moves to")
    from_state: Literal[runs.RUN_STATES] | None = Field(
        None, alias="from", description="the state the worker believes the run is in; 409 when it is in another"
    )
    session_id: str | None = Field(None, min_length=1, max_length=200, pattern=LINE)
    commit_sha: str | None = Field(None, pattern=OBJECT_NAME)
    diffstat: Diffstat | None = None
    verify: list[VerifyResult] | None = Field(None, max_length=50, description="each verify command the daemon ran")
    usage: dict | None = None
    error: str | None = Field(None, min_length=1, max_length=2000)
    summary: str | None = Field(None, max_length=8000, description="the agent's summary, for the evidence")

    @model_validator(mode="after")
    def _bounded(self):
        if self.usage is not None and _json_bytes(self.usage) > MAX_REPORT_BYTES:
            raise ValueError(f"usage takes at most {MAX_REPORT_BYTES} bytes of JSON")
        return self


# Reading runs

RUN_VIEW = """
SELECT r.id, p.name, r.plan_id, r.step_key, r.plan_revision, u.login, r.worker_id, w.name, r.pinned_worker_id,
       r.requested_runtime, r.runtime, r.mode, r.approval, r.timeout_s / 60, r.attempt, r.max_attempts,
       r.parent_run_id, r.state, r.lease_expires_at, r.session_id, r.repo, r.branch, r.commit_sha, r.diffstat,
       r.verify, r.evidence, r.usage, r.error, r.cancel_requested_at, r.queued_at, r.leased_at, r.started_at,
       r.finished_at
  FROM runs r JOIN projects p ON p.id = r.project_id JOIN users u ON u.id = r.dispatched_by
  LEFT JOIN workers w ON w.id = r.worker_id
 WHERE r.id = ANY(%s)
 ORDER BY r.id
"""


async def run_views(conn, run_ids: list[int]) -> list[Run]:
    rows = await (await conn.execute(RUN_VIEW, (list(run_ids),))).fetchall()
    return [Run(**dict(zip(Run.model_fields, row, strict=True))) for row in rows]


async def run_view(conn, run_id: int) -> Run:
    (found,) = await run_views(conn, [run_id])
    return found


ACTIVE_RUNS = """
SELECT r.step_key, r.id, r.state, u.login
  FROM runs r JOIN users u ON u.id = r.dispatched_by
 WHERE r.project_id = %s AND r.plan_id = %s AND r.state = ANY(%s)
"""


async def _active_runs(conn, project_id: int, plan_id: str) -> dict[str, ActiveRun]:
    rows = await (await conn.execute(ACTIVE_RUNS, (project_id, plan_id, list(runs.ACTIVE_STATES)))).fetchall()
    return {key: ActiveRun(id=run_id, state=state, dispatched_by=login) for key, run_id, state, login in rows}


def _text_or_none(value) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _busy(active: ActiveRun) -> str:
    return f"it has run #{active.id}, {active.state}, dispatched by {active.dispatched_by}"


@router.get(
    "/{project}/plans/{plan_id}/ready-steps",
    response_model=ReadySteps,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def ready_steps(
    request: Request,
    project: ProjectName,
    plan_id: plan_routes.PlanId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> ReadySteps:
    """Every step of the plan with whether it may be dispatched now, and why not."""
    async with request.app.state.pool.connection() as conn:
        access = await project_access(conn, user, project)
        plan_routes._reader(access)
        held = await plan_routes._visible(conn, access, plan_id, sink)
        active = await _active_runs(conn, access.project_id, plan_id)
    body = held.body
    steps = body.get("steps") if isinstance(body.get("steps"), list) else []
    shown = []
    for index, step in enumerate(steps):
        key = step_key(step, index)
        reason = runs.unready_reason(body, step)
        running = active.get(key)
        if reason is None and running is not None:
            reason = _busy(running)
        mapping = step if isinstance(step, dict) else {}
        repo = runs.plan_repo(body, mapping) if isinstance(step, dict) else None
        shown.append(
            StepReadiness(
                key=key,
                title=_text_or_none(mapping.get("title")),
                repo=_text_or_none(repo.get("repo")) if repo else None,
                status=_text_or_none(mapping.get("status", "pending")) if isinstance(step, dict) else None,
                ready=reason is None,
                reason=reason,
                active_run=running,
            )
        )
    return ReadySteps(project=project, plan_id=plan_id, revision=held.revision, steps=shown)


# Dispatching


def _dispatcher(access: ProjectAccess) -> None:
    if not has_role(access.role, "writer"):
        held = access.role or "no grant"
        raise HTTPException(
            403, f"dispatching runs in project {access.name} needs the writer role on it; you hold {held}"
        )


PINNABLE = """
SELECT w.owner_id, w.name, w.revoked_at,
       EXISTS (SELECT 1 FROM worker_projects wp WHERE wp.worker_id = w.id AND wp.project_id = %s)
  FROM workers w WHERE w.id = %s
"""


async def _pinnable(conn, user: Principal, access: ProjectAccess, worker_id: int) -> None:
    """403 unless worker ``worker_id`` is ``user``'s own; 409 when it is revoked or does not serve the project."""
    row = await (await conn.execute(PINNABLE, (access.project_id, worker_id))).fetchone()
    if row is None or row[0] != user.user_id:  # the same answer for another member's worker and for no worker
        raise HTTPException(
            403, f"a run goes only to a worker of the member who dispatches it, and you have no worker {worker_id}"
        )
    _, name, revoked_at, serves = row
    if revoked_at is not None:
        raise HTTPException(409, f"worker {name} was revoked at {revoked_at.isoformat()}; it takes no runs")
    if not serves:
        raise HTTPException(409, f"worker {name} does not take runs of project {access.name}: register it for it")


INSERT_RUN = """
INSERT INTO runs (project_id, plan_id, step_key, plan_revision, dispatched_by, pinned_worker_id, requested_runtime,
                  runtime, mode, approval, timeout_s, parent_run_id, repo, branch)
VALUES (%(project)s, %(plan)s, %(step)s, %(revision)s, %(user)s, %(pinned)s, %(runtime)s, %(runtime)s, %(mode)s,
        %(approval)s, %(timeout)s, %(parent)s, %(repo)s, %(branch)s)
RETURNING id
"""


def _short(value) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


async def _queue_run(
    conn,
    access: ProjectAccess,
    user: Principal,
    held,
    key: str,
    active: dict[str, ActiveRun],
    *,
    runtime: str,
    mode: str,
    approval: str,
    timeout_s: int,
    pinned: int | None,
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
        reason = _busy(active[key])
    if reason is not None:
        raise HTTPException(409, f"step {key} of plan {held.plan_id} is not ready: {reason}; nothing was dispatched")
    repo = runs.plan_repo(body, step) or {}
    name = _short(repo.get("repo"))
    if name is None:
        raise HTTPException(
            422, f"step {key} names no repo and the plan does not list exactly one: give the step a repo"
        )
    params = {
        "project": access.project_id,
        "plan": held.plan_id,
        "step": key,
        "revision": held.revision,
        "user": user.user_id,
        "pinned": pinned,
        "runtime": runtime,
        "mode": mode,
        "approval": approval,
        "timeout": timeout_s,
        "parent": parent,
        "repo": name,
        "branch": _short(repo.get("branch")),
    }
    try:
        async with conn.transaction():
            run_id = (await (await conn.execute(INSERT_RUN, params)).fetchone())[0]
    except psycopg.errors.UniqueViolation:  # another dispatch of the step got in first
        raise HTTPException(409, f"step {key} of plan {held.plan_id} has an active run already") from None
    except psycopg.errors.CheckViolation:
        raise HTTPException(422, f"step {key}: its repo or branch name is not one a run can hold") from None
    active[key] = ActiveRun(id=run_id, state="queued", dispatched_by=user.login)
    await notify_queued(conn, run_id)
    return run_id


def _run_target(project: str, plan_id: str, key: str, run_id: int) -> str:
    return f"{project}/{plan_id}#{key} run:{run_id}"


@router.post(
    "/{project}/runs",
    status_code=201,
    response_model=list[Run],
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def dispatch(request: Request, project: ProjectName, body: Dispatch, user: CurrentUser) -> list[Run]:
    """Queue a run of each step named, all of them or none; each one goes to a worker of the caller."""
    keys = list(dict.fromkeys(str(step) for step in body.steps))
    async with request.app.state.pool.connection() as conn:
        access = await project_access(conn, user, project)
        _dispatcher(access)
        held = await plan_routes._visible(conn, access, body.plan_id, None)
        if body.worker_id is not None:
            await _pinnable(conn, user, access, body.worker_id)
        active = await _active_runs(conn, access.project_id, body.plan_id)
        queued = []
        for key in keys:
            run_id = await _queue_run(
                conn,
                access,
                user,
                held,
                key,
                active,
                runtime=body.runtime,
                mode=body.mode,
                approval=body.approval,
                timeout_s=body.timeout_min * 60,
                pinned=body.worker_id,
            )
            target = _run_target(project, body.plan_id, key, run_id)
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


# The owner's controls

OWNED_RUN = """
SELECT r.dispatched_by, u.login, r.state, r.plan_id, r.step_key, r.cancel_requested_at, r.requested_runtime, r.mode,
       r.approval, r.timeout_s, r.pinned_worker_id
  FROM runs r JOIN users u ON u.id = r.dispatched_by
 WHERE r.id = %s AND r.project_id = %s
   FOR UPDATE OF r
"""


async def _owned_run(conn, user: Principal, project: str, run_id: int, action: str):
    """(access, row of OWNED_RUN) for a run of ``project`` that ``user`` dispatched, its row locked; 404 for a run
    ``user`` cannot see, 403 for another member's."""
    access = await project_access(conn, user, project)
    plan_routes._reader(access)
    row = await (await conn.execute(OWNED_RUN, (run_id, access.project_id))).fetchone()
    if row is None:
        raise HTTPException(404, f"project {project} has no run {run_id}: see GET /v1/projects/{project}/runs")
    if row[0] != user.user_id:
        raise HTTPException(403, f"only {row[1]}, who dispatched run {run_id}, may {action} it")
    return access, row


async def _audit_run(conn, user: Principal, access: ProjectAccess, action: str, target: str) -> None:
    await audit.record(
        conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target, project_id=access.project_id
    )


@router.post("/{project}/runs/{run_id}/cancel", response_model=Run, responses=REFUSALS)
async def cancel(request: Request, project: ProjectName, run_id: RunId, user: CurrentUser) -> Run:
    """Cancel a queued run or one in review at once; ask the worker holding a held run to stop it."""
    async with request.app.state.pool.connection() as conn:
        access, row = await _owned_run(conn, user, project, run_id, "cancel")
        state, plan_id, key, cancel_requested_at = row[2], row[3], row[4], row[5]
        if state in runs.TERMINAL_STATES:
            raise HTTPException(409, f"run {run_id} is {state}, which is final")
        if state in runs.HELD_STATES:
            if cancel_requested_at is not None:  # asked already: nothing changes, nothing is audited
                return await run_view(conn, run_id)
            await conn.execute("UPDATE runs SET cancel_requested_at = now() WHERE id = %s", (run_id,))
        else:
            reason = f"{user.login} cancelled it"
            await move_run(conn, run_id, state, "cancelled", "owner", reason=reason, token_id=user.token_id)
        await _audit_run(conn, user, access, audit.RUN_CANCEL, _run_target(project, plan_id, key, run_id))
        view = await run_view(conn, run_id)
    log.info("run cancelled", extra={"run_id": run_id, "state": view.state, "login": user.login})
    return view


@router.post("/{project}/runs/{run_id}/approve", response_model=Run, responses=REFUSALS)
async def approve(request: Request, project: ProjectName, run_id: RunId, user: CurrentUser) -> Run:
    """Approve a run in review: the run is done, and so is its step."""
    async with request.app.state.pool.connection() as conn:
        access, row = await _owned_run(conn, user, project, run_id, "approve")
        _dispatcher(access)
        state, plan_id, key = row[2], row[3], row[4]
        if state != "review":
            raise HTTPException(409, f"run {run_id} is {state}; only a run in review is approved")
        await move_run(conn, run_id, state, "done", "owner", reason=f"{user.login} approved it", token_id=user.token_id)
        await _audit_run(conn, user, access, audit.RUN_APPROVE, _run_target(project, plan_id, key, run_id))
        view = await run_view(conn, run_id)
    log.info("run approved", extra={"run_id": run_id, "login": user.login})
    return view


@router.post("/{project}/runs/{run_id}/rerun", status_code=201, response_model=Run, responses=REFUSALS)
async def rerun(request: Request, project: ProjectName, run_id: RunId, user: CurrentUser) -> Run:
    """Queue the step of a run that ended again, with the same runtime, mode, approval, timeout and worker, at the
    plan's current revision."""
    async with request.app.state.pool.connection() as conn:
        access, row = await _owned_run(conn, user, project, run_id, "rerun")
        _dispatcher(access)
        _, _, state, plan_id, key, _, runtime, mode, approval, timeout_s, pinned = row
        if state not in runs.TERMINAL_STATES:
            raise HTTPException(409, f"run {run_id} is still {state}: a run is rerun once it has ended")
        held = await plan_routes._visible(conn, access, plan_id, None)
        if pinned is not None:
            await _pinnable(conn, user, access, pinned)
        active = await _active_runs(conn, access.project_id, plan_id)
        new_id = await _queue_run(
            conn,
            access,
            user,
            held,
            key,
            active,
            runtime=runtime,
            mode=mode,
            approval=approval,
            timeout_s=timeout_s,
            pinned=pinned,
            parent=run_id,
        )
        target = f"{_run_target(project, plan_id, key, new_id)} rerun of run:{run_id}"
        await _audit_run(conn, user, access, audit.RUN_RERUN, target)
        view = await run_view(conn, new_id)
    log.info("run rerun", extra={"run_id": new_id, "rerun_of": run_id, "login": user.login})
    return view


# The worker's side


class RunWakeups:
    """The api process's one LISTEN on RUNS_CHANNEL, opened by the first claim, and the claims waiting on it. A
    notification, or a newer claim of the same worker, wakes every waiting claim, which then looks at the queue
    again; a claim also looks every CLAIM_POLL_SECONDS, so it never depends on the connection being up."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._task: asyncio.Task | None = None
        self._condition = asyncio.Condition()
        self.generation = 0  # counts wake-ups; a claim waits only while it has not changed
        self._tickets: dict[int, int] = {}  # worker id: the number of its newest claim

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._listen(), name="evo-hub-run-wakeups")

    async def _listen(self) -> None:
        backoff = LISTEN_BACKOFF[0]
        while True:
            try:
                conn = await psycopg.AsyncConnection.connect(
                    self._dsn, autocommit=True, application_name=APPLICATION_NAME, connect_timeout=CONNECT_TIMEOUT
                )
                async with conn:
                    await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(RUNS_CHANNEL)))
                    backoff = LISTEN_BACKOFF[0]
                    await self.wake()  # a run queued while nobody listened is found by this look
                    async for _ in conn.notifies():
                        await self.wake()
            except (psycopg.Error, OSError) as exc:
                log.warning(
                    "cannot listen for queued runs; claims look every few seconds instead",
                    extra={"error": type(exc).__name__, "retry_s": backoff},
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, LISTEN_BACKOFF[1])

    async def wake(self) -> None:
        self.generation += 1
        async with self._condition:
            self._condition.notify_all()

    async def wait(self, seen: int, timeout: float) -> None:
        """Return once something woke the claims after generation ``seen``, or after ``timeout`` seconds."""
        async with self._condition:
            if self.generation != seen:
                return
            try:
                await asyncio.wait_for(self._condition.wait(), timeout)
            except asyncio.TimeoutError:  # not TimeoutError itself before Python 3.11
                pass

    async def ticket(self, worker_id: int) -> int:
        """The number of a new claim of ``worker_id``, which makes any older one of it end without a run."""
        number = self._tickets.get(worker_id, 0) + 1
        self._tickets[worker_id] = number
        await self.wake()
        return number

    def current(self, worker_id: int, number: int) -> bool:
        return self._tickets.get(worker_id) == number

    def done(self, worker_id: int, number: int) -> None:
        if self._tickets.get(worker_id) == number:
            del self._tickets[worker_id]

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


WORKER_OF_TOKEN = """
SELECT id, owner_id, name, slots, runtimes, checkouts, drained_at, revoked_at
  FROM workers WHERE token_id = %s
   FOR UPDATE
"""


async def _worker_of(conn, user: Principal):
    """The row of the worker whose token made the request, locked; 403 when there is none."""
    row = await (await conn.execute(WORKER_OF_TOKEN, (user.token_id,))).fetchone()
    if row is None or row[7] is not None:
        raise HTTPException(403, "this worker token belongs to no live worker: join the machine again")
    return row


SERVED = """
SELECT wp.project_id, p.name
  FROM worker_projects wp JOIN projects p ON p.id = wp.project_id
 WHERE wp.worker_id = %(worker)s
   AND EXISTS (SELECT 1 FROM grants g
                WHERE g.user_id = %(owner)s AND g.project_id = wp.project_id AND g.role = ANY(%(writers)s))
"""
CLAIMABLE = """
SELECT r.id, r.runtime
  FROM runs r
 WHERE r.state = 'queued' AND r.project_id = ANY(%(projects)s) AND r.dispatched_by = %(owner)s
   AND (r.pinned_worker_id IS NULL OR r.pinned_worker_id = %(worker)s)
   AND (r.runtime = 'any' OR r.runtime = ANY(%(runtimes)s))
   AND (r.project_id, r.repo) IN (SELECT * FROM unnest(%(pids)s::bigint[], %(repos)s::text[]))
 ORDER BY r.id
 LIMIT 1
   FOR UPDATE SKIP LOCKED
"""
HELD_COUNT = "SELECT count(*) FROM runs WHERE worker_id = %s AND state = ANY(%s)"
REVISION_BODY = "SELECT body FROM plan_revisions WHERE project_id = %s AND plan_id = %s AND revision = %s"


async def _try_claim(pool, user: Principal) -> RunSpec | None:
    """Lease the run the worker of ``user`` may take now, if any (see the module's docstring)."""
    async with pool.connection() as conn:
        worker_id, owner_id, name, slots, reported, checkouts, drained_at, _ = await _worker_of(conn, user)
        if drained_at is not None:
            return None
        held = (await (await conn.execute(HELD_COUNT, (worker_id, list(runs.HELD_STATES)))).fetchone())[0]
        if held >= slots:
            return None
        runtimes = [runtime for runtime in runs.RUNTIMES if available((reported or {}).get(runtime))]
        if not runtimes:
            return None
        params = {"worker": worker_id, "owner": owner_id, "writers": WRITER_ROLES}
        served = dict(await (await conn.execute(SERVED, params)).fetchall())
        by_name = {project: project_id for project_id, project in served.items()}
        pairs = []
        for checkout in checkouts or {}:
            project, _, repo = checkout.partition("/")
            if project in by_name and repo:
                pairs.append((by_name[project], repo))
        if not pairs:
            return None
        params |= {
            "projects": list(served),
            "runtimes": runtimes,
            "pids": [pid for pid, _ in pairs],
            "repos": [repo for _, repo in pairs],
        }
        row = await (await conn.execute(CLAIMABLE, params)).fetchone()
        if row is None:
            return None
        run_id, asked = row
        runtime = runtimes[0] if asked == "any" else asked
        await move_run(
            conn,
            run_id,
            "queued",
            "leased",
            "worker",
            reason=f"worker {name} claimed it",
            columns={"worker_id": worker_id, "runtime": runtime},
            token_id=user.token_id,
        )
        spec = await _run_spec(conn, run_id)
    log.info("run claimed", extra={"run_id": run_id, "worker_id": worker_id, "runtime": runtime})
    return spec


async def _run_spec(conn, run_id: int) -> RunSpec:
    view = await run_view(conn, run_id)
    project_id = (await (await conn.execute("SELECT project_id FROM runs WHERE id = %s", (run_id,))).fetchone())[0]
    row = await (await conn.execute(REVISION_BODY, (project_id, view.plan_id, view.plan_revision))).fetchone()
    plan = row[0] if row else {"id": view.plan_id, "steps": []}
    try:
        step = plan["steps"][step_index(plan, view.step_key)]
    except (PlanProblem, KeyError, TypeError):
        step = None
    if not isinstance(step, dict):
        step = {"id": view.step_key}
    prompt = runs.build_prompt(plan, step, {"repo": view.repo, "branch": view.branch})
    return RunSpec(
        id=view.id,
        project=view.project,
        plan_id=view.plan_id,
        step_key=view.step_key,
        title=_text_or_none(step.get("title")),
        plan_revision=view.plan_revision,
        attempt=view.attempt,
        max_attempts=view.max_attempts,
        parent_run_id=view.parent_run_id,
        runtime=view.runtime,
        mode=view.mode,
        approval=view.approval,
        timeout_min=view.timeout_min,
        repo=view.repo,
        branch=view.branch,
        lease_expires_at=view.lease_expires_at,
        prompt=prompt,
    )


@worker_router.post("/claim", response_model=Claim, responses={403: {"model": ErrorBody}})
async def claim(request: Request, user: CurrentUser, body: ClaimRequest | None = None) -> Claim:
    """Wait up to ``wait_s`` seconds (25 by default) for a run this worker may take, and lease it."""
    wait = (body or ClaimRequest()).wait_s
    wakeups: RunWakeups = request.app.state.run_wakeups
    wakeups.start()
    pool = request.app.state.pool
    async with pool.connection() as conn:
        worker_id = (await _worker_of(conn, user))[0]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    number = await wakeups.ticket(worker_id)
    try:
        while True:
            seen = wakeups.generation
            spec = await _try_claim(pool, user)
            if spec is not None:
                return Claim(run=spec)
            remaining = deadline - loop.time()
            if remaining <= 0 or not wakeups.current(worker_id, number) or await request.is_disconnected():
                return Claim(run=None)
            await wakeups.wait(seen, min(remaining, CLAIM_POLL_SECONDS))
    finally:
        wakeups.done(worker_id, number)


EXTEND = """
UPDATE runs SET lease_expires_at = now() + %(lease)s
 WHERE worker_id = %(worker)s AND id = ANY(%(ids)s) AND state = ANY(%(held)s)
RETURNING id, state, cancel_requested_at IS NOT NULL, lease_expires_at
"""
INBOX = """
SELECT run_id, count(*) FROM run_inbox WHERE run_id = ANY(%s) AND delivered_at IS NULL GROUP BY run_id
"""
RECORD_HEARTBEAT = """
UPDATE workers SET last_heartbeat_at = now(), runtimes = %(runtimes)s, checkouts = %(checkouts)s,
       free_slots = least(%(free)s, slots), agent_version = coalesce(%(version)s, agent_version)
 WHERE id = %(worker)s
"""


@worker_router.post("/heartbeat", response_model=HeartbeatAnswer, responses={403: {"model": ErrorBody}})
async def heartbeat(request: Request, body: HeartbeatRequest, user: CurrentUser) -> HeartbeatAnswer:
    """Record the machine, extend the leases of the runs it holds, and say what it should do with them."""
    reported = list(dict.fromkeys(body.runs))
    runtimes, checkouts = body.stored()
    async with request.app.state.pool.connection() as conn:
        worker_id, _, _, _, _, _, drained_at, _ = await _worker_of(conn, user)
        await conn.execute(
            RECORD_HEARTBEAT,
            {
                "worker": worker_id,
                "runtimes": Jsonb(runtimes),
                "checkouts": Jsonb(checkouts),
                "free": body.free_slots,
                "version": body.agent_version,
            },
        )
        params = {"lease": LEASE, "worker": worker_id, "ids": reported, "held": list(runs.HELD_STATES)}
        extended = {row[0]: row[1:] for row in await (await conn.execute(EXTEND, params)).fetchall()}
        waiting = dict(await (await conn.execute(INBOX, (list(extended),))).fetchall()) if extended else {}
    controls = []
    for run_id in reported:
        if run_id in extended:
            state, cancel, lease = extended[run_id]
            controls.append(
                RunControl(
                    id=run_id,
                    held=True,
                    state=state,
                    lease_expires_at=lease,
                    cancel=cancel,
                    inbox=waiting.get(run_id, 0),
                )
            )
        else:
            controls.append(RunControl(id=run_id, held=False, state=None, lease_expires_at=None, cancel=True))
    return HeartbeatAnswer(drain=drained_at is not None, runs=controls)


SAME_COLUMNS = ("session_id", "commit_sha", "diffstat", "verify", "usage")  # what a report without a move may set
SAME_STATE = """
UPDATE runs SET session_id = coalesce(%(session_id)s, session_id), commit_sha = coalesce(%(commit_sha)s, commit_sha),
       diffstat = coalesce(%(diffstat)s, diffstat), verify = coalesce(%(verify)s, verify),
       usage = coalesce(%(usage)s, usage)
 WHERE id = %(id)s
"""
REPORTED_RUN = "SELECT state, approval, worker_id, cancel_requested_at FROM runs WHERE id = %s FOR UPDATE"


def _reported_columns(body: StateReport) -> dict:
    columns = {
        "session_id": body.session_id,
        "commit_sha": body.commit_sha,
        "diffstat": body.diffstat.model_dump() if body.diffstat else None,
        "verify": [item.model_dump(exclude_none=True) for item in body.verify] if body.verify is not None else None,
        "usage": body.usage,
    }
    return {name: value for name, value in columns.items() if value is not None}


@worker_router.post("/runs/{run_id}/state", response_model=Run, responses=REFUSALS)
async def report_state(request: Request, run_id: RunId, body: StateReport, user: CurrentUser) -> Run:
    """Move a run this worker holds, as the transition table lets a worker."""
    async with request.app.state.pool.connection() as conn:
        worker_id, _, name, *_ = await _worker_of(conn, user)
        row = await (await conn.execute(REPORTED_RUN, (run_id,))).fetchone()
        if row is None or row[2] != worker_id:
            raise HTTPException(404, NOT_HELD.format(id=run_id))
        state, approval, _, cancel_requested_at = row
        columns = _reported_columns(body)
        if state == body.state:  # a resend, or news without a move: kept, and nothing moves
            values = {
                name: Jsonb(value) if isinstance(value, (dict, list)) else value for name, value in columns.items()
            }
            await conn.execute(SAME_STATE, {"id": run_id, **{name: values.get(name) for name in SAME_COLUMNS}})
            return await run_view(conn, run_id)
        if body.from_state is not None and body.from_state != state:
            raise HTTPException(409, f"run {run_id} is {state}, not {body.from_state}")
        if state not in runs.HELD_STATES:
            raise HTTPException(404, NOT_HELD.format(id=run_id))
        try:
            runs.check_transition(state, body.state, "worker")
        except runs.TransitionRefused as exc:
            raise HTTPException(409, f"run {run_id}: {exc}") from None
        _check_verdict(run_id, approval, body)
        error = body.error
        if body.state == "failed" and error is None:
            error = f"worker {name} reported that the run failed"
        if body.state in ("done", "review"):
            found = await run_step(conn, run_id)
            found = replace(
                found,
                commit_sha=columns.get("commit_sha", found.commit_sha),
                diffstat=columns.get("diffstat", found.diffstat),
                verify=columns.get("verify", found.verify),
            )
            columns["evidence"] = evidence(found, body.summary)
        if body.state == "cancelled":
            reason = "its owner asked to cancel it" if cancel_requested_at else f"worker {name} stopped it"
        elif error:
            reason = error
        else:
            reason = f"worker {name} reported {body.state}"
        await move_run(
            conn,
            run_id,
            state,
            body.state,
            "worker",
            reason=reason,
            error=error,
            columns=columns,
            token_id=user.token_id,
        )
        view = await run_view(conn, run_id)
    log.info("run moved", extra={"run_id": run_id, "from": state, "to": body.state, "worker_id": worker_id})
    return view


def _check_verdict(run_id: int, approval: str, body: StateReport) -> None:
    """409 for a verdict the run's approval does not allow."""
    if body.state == "done":
        if approval != "auto":
            raise HTTPException(409, f"run {run_id} waits for its owner's approval: report review, not done")
        if not body.verify or any(item.exit_code != 0 for item in body.verify):
            raise HTTPException(
                409,
                f"run {run_id} is done only when every verify command exited 0; report the results, or failed",
            )
    if body.state == "review" and approval != "review":
        raise HTTPException(409, f"run {run_id} has approval auto: report done or failed, not review")
