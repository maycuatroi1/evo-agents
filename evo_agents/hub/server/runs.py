"""Runs on the hub: the steps of a plan that may be dispatched, dispatching them, listing and showing runs, the
worker's side of the queue (claim, heartbeat, state) and the owner's cancel, approve, rerun, takeover and handback.
``docs/workers.md`` is the protocol, ``run_state`` moves the runs and records each move in the plan, and
``run_events`` holds a run's log, stream, messages and blobs.

GET /v1/projects/{p}/plans/{plan}/ready-steps (reader) lists every step of the plan, ready or not, with why it is not
(``runs.unready_reason``, or the active run it has). POST /v1/projects/{p}/runs (writer) queues one run per step
named: every step must be ready and without an active run (409 otherwise, and nothing is queued), and a worker named
must be one of the caller's own (403 for any other id, a hub admin's included), live and serving the project (409).
Each run is notified on RUNS_CHANNEL and audited (run.dispatch), and keeps the step's title. ``model`` is optional,
one line of at most ``runs.MAX_MODEL_CHARS``, as the runtime names it; without one the runtime chooses as it would.
Each run keeps the credential it was dispatched with in dispatched_via (``credentials.dispatch_credential``): web for a
web session, machine for a token. A worker whose owner set its dispatch_from to web takes runs dispatched from a web
session only: a dispatch, plan run or rerun pinned to it with a token gets 403 saying so, and its claims pass over
the runs dispatched with a token.

POST /v1/projects/{p}/plan-runs (writer) queues a plan run (``runs.RUN_KINDS``): one run, on one worker of the
caller's, that does every step of the plan not done yet. It is refused with 409 when the plan has no pending step,
when the plan has an active run of any kind, or when a step not done names no repo and the plan does not list exactly
one; the run's repos are those of the steps not done, each with the branch the plan's repos name. A dispatch of steps
and a dispatch of a plan run take the same transaction-scoped advisory lock of the plan (``plan_lock_key``) before
they look at its active runs, so the two exclude each other: a step of a plan with an active plan run gets 409 ("plan X
has plan run #N"), and a plan run waits until no run of the plan's steps is active. Each plan run is audited
(run.dispatch_plan). ready-steps names the plan's active plan run (``plan_run``), and no step is ready while it is.

GET /v1/projects/{p}/runs (reader) lists the project's runs newest first, filtered by state, plan, step, worker,
dispatcher and text, a page at a time, with how many runs each state has under the other filters; GET .../runs/{id}
shows one. A reader sees the runs of the plans it may read through the sink it names (``visible_plans``), and a run
of another plan reads as no run (``readable_run``). GET .../runs/stats counts the runs of the same plans that ended on
each of the last ``days`` UTC days (MIN_STATS_DAYS to MAX_STATS_DAYS, STATS_DAYS by default) in each end state, with
the 50th and 90th percentile of how long they ran (started, else leased, to finished, as the runs pages count it) and
the tokens of their usage, read as the run page's usage card reads it (``_usage_parts``); a day without a run has
zeros.

A worker claims with POST /v1/worker/claim, which waits up to CLAIM_WAIT_SECONDS: ``RunWakeups`` wakes it when
RUNS_CHANNEL is notified, on the api process's one LISTEN connection (``listen``, opened by the first claim or stream),
and it looks again every CLAIM_POLL_SECONDS besides, so a lost notification delays a claim but never loses it. A claim
takes the oldest queued run, ``FOR UPDATE SKIP LOCKED``, that its owner dispatched, of a project the worker serves and
on which the owner still holds writer, asking for a runtime the worker reported (``any`` takes the first of
runs.RUNTIMES it has), of a repo it has a checkout of, pinned to no other worker, dispatched from a web session when the
worker's dispatch_from is web, while the worker is neither draining nor revoked and holds fewer runs than its slots. A
worker has one claim waiting at a time: a newer claim answers the older one with no run. The claimed run is leased for
EVO_HUB_RUN_LEASE_SECONDS (LEASE_SECONDS by default) and comes with its prompt (``runs.build_prompt`` over the plan
revision it was dispatched from). A plan run needs a checkout of every repo in its repos and a daemon of
runs.PLAN_RUN_AGENT or later, and comes with ``runs.build_plan_prompt`` and the plan at the hub's current revision,
which the daemon writes to ``runs.PLAN_FILE``. A dispatch pinned to a worker whose last heartbeat says it could never
claim the run gets 409 (``_fits``). A claim whose worker hung up (a daemon stopping drops the claim it waits on) takes
nothing: it ends before it looks at the queue again, and a run it leased in the meantime is rolled back before the
transaction commits, so the run stays queued for the next claim instead of waiting out a lease nobody holds.

POST /v1/worker/heartbeat records the machine (runtimes, each with the models it lists when it lists any, checkouts
keyed ``<project>/<repo>``, free slots), extends the lease of every run the worker names and still holds by the same
time, settles their agent time (``run_state.SETTLE``), and answers with control: per run, whether to cancel (asked by
the owner, or a run the worker no longer holds), takeover, handback, terminal_open (a browser waits for the worker's
end of the run's terminal, ``terminal.Terminals.waiting``), how many inbox messages wait and how many decisions of the
run are open; for the worker, whether to drain. A run the reaper parked, or one done because a new run resumes it,
comes back with held false, cancel false and park true: the worker stops its agent at the end of the turn, keeps the
session and the worktrees, and frees the slot.

POST /v1/worker/runs/{id}/state reports a move of a run the worker holds (404 otherwise), checked against
``runs.TRANSITIONS`` with the worker as actor (409 otherwise): ``done`` only for approval auto with every verify
command exited 0, ``review`` only for approval review. A plan run ends ``done`` without verify results, since each of
its steps was verified when it was reported, and never in review. Reporting the state the run is in already changes
nothing but the session id, commit, diffstat, verify results and usage given, so a resend is safe. A plan run reports
``waiting`` when its agent's turn ended with a decision open, which needs a decision of the run that is open or whose
answer the worker has not taken yet (409 otherwise: waiting does not count toward the timeout), and ``running`` once
the answer reached the agent. A move out of the held states gives back the run's leases (``credentials``), and their
GitHub tokens are revoked once the move commits, before the answer.

The worker holding a plan run reads the plan as the hub holds it now with GET /v1/worker/runs/{id}/plan, and reports
each step with POST /v1/worker/runs/{id}/steps/{key}: ``in_progress``, ``done`` (with at least one verify result,
every one exited 0; 422 otherwise) or ``pending``, with evidence, the repo, the verify results and the commit. The hub
writes the step as the member who dispatched the run (``run_state.write_step``), answers with the error the plan's
write gets when it cannot, never sets a done step back (409), keeps the report as a ``system`` event of the run, and
audits it (run.step_report). A step the plan does not have, a run of one step, or a run the worker does not hold gets
404. A plan run that ends ``done`` writes no step; one that fails or is cancelled sets the steps it left
``in_progress`` back to ``pending`` (``run_state.release_plan_steps``).

The owner of a run is the member who dispatched it. Cancel moves a queued run or one in review to ``cancelled`` and
asks the worker holding a held run to stop it (the next heartbeat says cancel); approve moves a run in review to
``done``; rerun queues the step again, at the plan's current revision, with the run's model, after a run that ended.
Takeover asks the worker holding a leased or running run to let a person drive the agent in a terminal, and handback
asks it to let an interactive run's agent go on headless: the next heartbeat says takeover or handback until the
worker reports interactive or running, and any other move drops the ask. Each one is audited (run.cancel,
run.approve, run.rerun, run.takeover, run.handback; an ask repeated is not); another member gets 403, someone without
a grant on the project 404.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Header, HTTPException, Path, Query, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from evo_agents.hub import runs
from evo_agents.hub.access import has_role
from evo_agents.hub.credentials import DISPATCHED_VIA, dispatch_credential
from evo_agents.hub.plans import PlanProblem, step_index, step_key
from evo_agents.hub.server import audit
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.listen import Listener
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.run_state import (
    MAX_EVIDENCE_BYTES,
    RUNS_CHANNEL,
    SETTLE,
    StepNotWritten,
    evidence,
    move_run,
    notify_queued,
    report_updates,
    run_step,
    write_event,
    write_step,
)
from evo_agents.hub.server.security import MACHINE, WEB, CurrentUser, Principal

log = logging.getLogger(__name__)

MAX_ID = 2**63 - 1  # bigint
MAX_DISPATCH_STEPS = 50
CLAIM_POLL_SECONDS = 5.0  # a waiting claim looks again this often, notification or not
MAX_HEARTBEAT_RUNS = 64
MAX_REPORT_BYTES = 64 * 1024  # runtimes and checkouts of a heartbeat, or the usage of a state report, as JSON
LINE = r"^[^\x00-\x1f\x7f]+$"
OBJECT_NAME = r"^([0-9a-f]{40}|[0-9a-f]{64})$"
CHECKOUT_KEY = r"^[a-z0-9][a-z0-9-]{0,99}/[^\x00-\x1f\x7f/][^\x00-\x1f\x7f]{0,199}$"  # <project>/<repo>
STEP_KEY_CHARS = 200
REQUESTED_RUNTIMES = ("any", *runs.RUNTIMES)
WRITER_ROLES = [role for role in ("reader", "writer", "admin") if has_role(role, "writer")]
NOT_HELD = "this worker does not hold run {id}: it may have been lost, cancelled or taken by another attempt"
STEP_REPORT_STATUSES = ("in_progress", "done", "pending")  # what a plan run's worker reports of a step
MAX_STEP_EVIDENCE_CHARS = MAX_EVIDENCE_BYTES  # the agent's evidence of one step; the hub's adds to it, within the bytes
PLAN_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"

router = APIRouter(prefix="/v1/projects", tags=["runs"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})

RunId = Annotated[int, Path(ge=1, le=MAX_ID)]
RuntimeName = Literal[runs.RUNTIMES]  # an alias: a model with a field named runs cannot say runs.RUNTIMES
ModelName = Annotated[str, Field(min_length=1, max_length=runs.MAX_MODEL_CHARS, pattern=LINE)]
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
    plan_run: ActiveRun | None = Field(
        None, description="the plan's active plan run: while there is one, no step of the plan is dispatched"
    )
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
    model: ModelName | None = Field(
        None,
        description="the model each run uses, as its runtime names it (opencode: provider/model); null: the "
        "runtime's own choice",
    )


class PlanRunDispatch(BaseModel):
    plan_id: str = Field(pattern=plan_routes.PLAN_ID)
    worker_id: int | None = Field(None, ge=1, le=MAX_ID, description="pin the run to this worker of yours")
    runtime: Literal[REQUESTED_RUNTIMES] = Field("any", description="any: the claiming worker picks one it has")
    model: ModelName | None = Field(
        None, description="the model to use, as its runtime names it (opencode: provider/model); null: the runtime's"
    )
    mode: Literal[runs.MODES] = "headless"
    timeout_h: Literal[runs.PLAN_TIMEOUT_CHOICES] = Field(
        4, description="hours of agent time the run may take; waiting for a decision and parked do not count"
    )


class RunRepo(BaseModel):
    """A repo of a plan run, with the branch the plan names for it."""

    repo: str
    branch: str | None = Field(None, description="null when the plan names none: the branch checked out")


class Run(BaseModel):
    id: int
    kind: Literal[runs.RUN_KINDS] = Field(description="step: one step of the plan; plan: every step not done yet")
    project: str
    plan_id: str
    step_key: str | None = Field(description="null for a plan run")
    title: str | None = Field(description="the step's title when the run was dispatched; the plan's for a plan run")
    plan_revision: int = Field(description="the plan revision the run was dispatched from")
    dispatched_by: str = Field(description="the login of the member who dispatched it, its owner")
    dispatched_via: Literal[DISPATCHED_VIA] | None = Field(
        description="the credential it was dispatched with: web, a web session; machine, a token (the command line, "
        "an agent); null for a run dispatched before 0.5.0"
    )
    worker_id: int | None = Field(description="the worker that claimed it")
    worker: str | None = Field(description="that worker's name")
    pinned_worker_id: int | None
    requested_runtime: Literal[REQUESTED_RUNTIMES]
    runtime: Literal[REQUESTED_RUNTIMES] = Field(description="any until a worker claims the run")
    model: str | None = Field(description="the model the dispatch asked for; null: the runtime's own choice")
    mode: Literal[runs.MODES]
    approval: Literal[runs.APPROVALS]
    timeout_min: int
    run_seconds: int = Field(description="the agent time the run has used of its timeout, as the hub last counted it")
    attempt: int
    max_attempts: int
    parent_run_id: int | None = Field(description="the run this one retries or reruns")
    resume_of_run_id: int | None = Field(description="the parked plan run this one goes on from, in its session")
    state: Literal[runs.RUN_STATES]
    lease_expires_at: datetime | None
    session_id: str | None
    repo: str | None = Field(description="null for a plan run, which has repos")
    branch: str | None
    repos: list[RunRepo] | None = Field(description="a plan run's repos; null for a run of one step")
    commit_sha: str | None
    diffstat: dict | None
    verify: list | None
    evidence: str | None
    usage: dict | None
    error: str | None
    log_sha256: str | None = Field(description="the blob of kind run-log its worker uploaded")
    diff_sha256: str | None = Field(description="the blob of kind run-diff: GET .../runs/{id}/diff")
    last_seq: int = Field(description="the seq of the run's latest event; 0 before the first")
    cancel_requested_at: datetime | None
    takeover_requested_at: datetime | None = Field(description="the owner asked to drive the agent in a terminal")
    handback_requested_at: datetime | None = Field(description="the owner asked to let the agent go on headless")
    queued_at: datetime
    leased_at: datetime | None
    started_at: datetime | None
    waiting_since: datetime | None = Field(
        description="when the agent's turn ended with a decision open, while waiting"
    )
    parked_at: datetime | None = Field(description="when the run was parked, for want of an answer")
    finished_at: datetime | None


class ClaimRequest(BaseModel):
    wait_s: float = Field(
        runs.CLAIM_WAIT_SECONDS, ge=0, le=runs.CLAIM_WAIT_SECONDS, description="how long to wait for a run"
    )


class PlanCopy(BaseModel):
    """A plan as the hub holds it at one revision."""

    revision: int
    body: dict


class RunSpec(BaseModel):
    id: int
    kind: Literal[runs.RUN_KINDS]
    project: str
    plan_id: str
    step_key: str | None = Field(description="null for a plan run")
    title: str | None
    plan_revision: int = Field(description="the plan revision the run was dispatched from")
    attempt: int
    max_attempts: int
    parent_run_id: int | None
    resume_of_run_id: int | None = Field(
        None, description="the parked plan run this one goes on from: reuse its worktrees and resume its session"
    )
    session_id: str | None = Field(None, description="the agent session to resume; null for a new one")
    runtime: Literal[runs.RUNTIMES]
    model: str | None = Field(description="the model the dispatch asked for; null: the runtime's own choice")
    mode: Literal[runs.MODES]
    approval: Literal[runs.APPROVALS]
    timeout_min: int
    repo: str | None = Field(description="null for a plan run")
    branch: str | None
    repos: list[RunRepo] | None = Field(description="a plan run's repos, each with a checkout on this worker")
    lease_expires_at: datetime
    prompt: str
    plan: PlanCopy | None = Field(
        description=f"a plan run's plan at the hub's current revision, for {runs.PLAN_FILE}; null for a run of one step"
    )


class Claim(BaseModel):
    run: RunSpec | None = Field(description="null when the wait ended without a run; claim again")


def _json_bytes(value) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())


class RuntimeReport(BaseModel):
    """One runtime as the daemon found it on the machine."""

    available: bool = Field(description="whether runs may use it; false with a reason when it is installed but not")
    version: str | None = Field(None, min_length=1, max_length=100, pattern=LINE)
    reason: str | None = Field(None, min_length=1, max_length=500, pattern=LINE, description="why it is unavailable")
    models: list[ModelName] | None = Field(
        None,
        max_length=runs.MAX_RUNTIME_MODELS,
        description="the models the runtime lists on the machine, for a dispatch to suggest; null when it lists none",
    )


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
    takeover: bool = Field(False, description="the owner asked to drive the agent in a terminal: report interactive")
    handback: bool = Field(False, description="the owner asked to let the agent go on headless: report running")
    terminal_open: bool = Field(
        False,
        description="a browser waits for the run's terminal: connect WS /v1/worker/runs/{id}/terminal once the run "
        "is interactive",
    )
    park: bool = Field(
        False,
        description="the run is parked (it waited too long for an answer), or done because a new run resumes it: stop "
        "the agent at the end of its turn, keep the session and the worktrees, free the slot",
    )
    inbox: int = Field(0, description="messages from the owner waiting for the agent: POST .../runs/{id}/inbox")
    decisions: int = Field(0, description="decisions of the run still open, waiting for the owner's answer")


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


class StepReport(BaseModel):
    """What a plan run's worker reports of one step of the plan."""

    status: Literal[STEP_REPORT_STATUSES] = Field(
        description="in_progress when the agent starts the step, done once verified, pending to hand it back"
    )
    repo: str | None = Field(
        None, min_length=1, max_length=STEP_KEY_CHARS, pattern=LINE, description="the run's repo the step was done in"
    )
    evidence: str | None = Field(
        None, min_length=1, max_length=MAX_STEP_EVIDENCE_CHARS, description="what the agent did and how it checked it"
    )
    verify: list[VerifyResult] | None = Field(
        None, max_length=50, description="each verify command the worker ran again; done needs one, every one exit 0"
    )
    commit_sha: str | None = Field(None, pattern=OBJECT_NAME, description="the repo's commit the step ends on")


class StepWritten(BaseModel):
    run_id: int
    plan_id: str
    step_key: str
    status: str | None = Field(description="the step's status as the plan holds it after the report")
    revision: int = Field(description="the plan's revision after the report")
    written: bool = Field(description="whether the report wrote a revision; false for a resend that changed nothing")


# Reading runs

RUN_COLUMNS = """
SELECT r.id, r.kind, p.name, r.plan_id, r.step_key, r.title, r.plan_revision, u.login, r.dispatched_via,
       r.worker_id, w.name, r.pinned_worker_id, r.requested_runtime, r.runtime, r.model, r.mode, r.approval,
       r.timeout_s / 60, r.run_seconds, r.attempt, r.max_attempts, r.parent_run_id, r.resume_of_run_id, r.state,
       r.lease_expires_at, r.session_id, r.repo, r.branch, r.repos, r.commit_sha, r.diffstat, r.verify, r.evidence,
       r.usage, r.error, r.log_sha256, r.diff_sha256, r.event_seq, r.cancel_requested_at, r.takeover_requested_at,
       r.handback_requested_at, r.queued_at, r.leased_at, r.started_at, r.waiting_since, r.parked_at, r.finished_at
  FROM runs r JOIN projects p ON p.id = r.project_id JOIN users u ON u.id = r.dispatched_by
  LEFT JOIN workers w ON w.id = r.worker_id
"""
RUN_VIEW = RUN_COLUMNS + " WHERE r.id = ANY(%s) ORDER BY r.id"


def _runs_of(rows) -> list[Run]:
    return [Run(**dict(zip(Run.model_fields, row, strict=True))) for row in rows]


async def run_views(conn, run_ids: list[int]) -> list[Run]:
    return _runs_of(await (await conn.execute(RUN_VIEW, (list(run_ids),))).fetchall())


async def run_view(conn, run_id: int) -> Run:
    (found,) = await run_views(conn, [run_id])
    return found


ACTIVE_RUNS = """
SELECT r.kind, r.step_key, r.id, r.state, u.login
  FROM runs r JOIN users u ON u.id = r.dispatched_by
 WHERE r.project_id = %s AND r.plan_id = %s AND r.state = ANY(%s)
 ORDER BY r.id
"""


@dataclass
class Activity:
    """The active runs of a plan: those of its steps by step key, and its plan run."""

    steps: dict[str, ActiveRun]
    plan_run: ActiveRun | None


async def _activity(conn, project_id: int, plan_id: str) -> Activity:
    rows = await (await conn.execute(ACTIVE_RUNS, (project_id, plan_id, list(runs.ACTIVE_STATES)))).fetchall()
    activity = Activity(steps={}, plan_run=None)
    for kind, key, run_id, state, login in rows:
        active = ActiveRun(id=run_id, state=state, dispatched_by=login)
        if kind == "plan":
            activity.plan_run = active
        else:
            activity.steps[key] = active
    return activity


def plan_lock_key(project_id: int, plan_id: str) -> str:
    """The text whose hash names the advisory lock a dispatch of a plan's steps or of its plan run takes."""
    return f"evo-runs:{project_id}:{plan_id}"


async def _lock_plan(conn, project_id: int, plan_id: str) -> None:
    """Take the plan's dispatch lock until the caller's transaction ends: a dispatch of its steps and one of its plan
    run then each see the other's run, never both none."""
    await conn.execute(PLAN_LOCK, (plan_lock_key(project_id, plan_id),))


def _text_or_none(value) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _busy(active: ActiveRun) -> str:
    return f"it has run #{active.id}, {active.state}, dispatched by {active.dispatched_by}"


def _plan_busy(plan_id: str, active: ActiveRun) -> str:
    return f"plan {plan_id} has plan run #{active.id}, {active.state}, dispatched by {active.dispatched_by}"


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
        activity = await _activity(conn, access.project_id, plan_id)
    body = held.body
    steps = body.get("steps") if isinstance(body.get("steps"), list) else []
    shown = []
    for index, step in enumerate(steps):
        key = step_key(step, index)
        reason = runs.unready_reason(body, step)
        running = activity.steps.get(key)
        if reason is None and running is not None:
            reason = _busy(running)
        if reason is None and activity.plan_run is not None:
            reason = _plan_busy(plan_id, activity.plan_run)
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
    return ReadySteps(project=project, plan_id=plan_id, revision=held.revision, plan_run=activity.plan_run, steps=shown)


# Listing and showing runs

MAX_LIST = 200
MAX_OFFSET = 100_000
StateCounts = create_model(
    "StateCounts",
    __doc__="Runs in each state.",
    **{state: (int, Field(0, ge=0)) for state in runs.RUN_STATES},
)


class RunList(BaseModel):
    runs: list[Run] = Field(description="newest first")
    total: int = Field(description="runs that match every filter")
    counts: StateCounts = Field(
        description="runs in each state that match the other filters: what the state filter would leave"
    )
    limit: int
    offset: int


PLAN_LABELS = "SELECT plan_id, label FROM plans WHERE project_id = %s"


async def visible_plans(conn, access: ProjectAccess, sink: str | None) -> list[str]:
    """The plans of the project whose runs the caller may read: those it may read through ``sink`` (the project's
    hub sink when None), by the plan's label. A run of a plan no longer on the hub is shown to nobody."""
    plan_routes._reader(access)
    through = plan_routes._sink(access, sink)
    rows = await (await conn.execute(PLAN_LABELS, (access.project_id,))).fetchall()
    return [plan_id for plan_id, label in rows if access.visible(label, through)]


READABLE_RUN = "SELECT plan_id FROM runs WHERE id = %s AND project_id = %s"


async def readable_run(conn, user: Principal, project: str, run_id: int, sink: str | None) -> ProjectAccess:
    """The caller's access to ``project`` when it may read run ``run_id`` of it: a grant on the project (404 without,
    403 for a hub admin without one) and the run's plan visible to it (404 otherwise, as for no run)."""
    access = await project_access(conn, user, project)
    plan_routes._reader(access)
    row = await (await conn.execute(READABLE_RUN, (run_id, access.project_id))).fetchone()
    if row is None or row[0] not in await visible_plans(conn, access, sink):
        raise HTTPException(404, f"project {project} has no run {run_id}: see GET /v1/projects/{project}/runs")
    return access


LIST_FILTERS = """
 WHERE r.project_id = %(project)s AND r.plan_id = ANY(%(plans)s)
   AND (%(plan)s::text IS NULL OR r.plan_id = %(plan)s)
   AND (%(step)s::text IS NULL OR r.step_key = %(step)s)
   AND (%(worker)s::bigint IS NULL OR r.worker_id = %(worker)s)
   AND (%(login)s::text IS NULL OR lower(u.login) = lower(%(login)s))
   AND (%(q)s::text IS NULL OR r.title ILIKE %(q)s OR r.step_key ILIKE %(q)s OR r.plan_id ILIKE %(q)s
        OR r.repo ILIKE %(q)s OR r.branch ILIKE %(q)s OR w.name ILIKE %(q)s OR u.login ILIKE %(q)s
        OR r.error ILIKE %(q)s OR r.id = %(run)s::bigint)
"""
LIST_PAGE = (
    RUN_COLUMNS
    + LIST_FILTERS
    + """   AND (cardinality(%(states)s::text[]) = 0 OR r.state = ANY(%(states)s))
 ORDER BY r.id DESC
 LIMIT %(limit)s OFFSET %(offset)s
"""
)
LIST_COUNTS = (
    """
SELECT r.state, count(*)
  FROM runs r JOIN users u ON u.id = r.dispatched_by LEFT JOIN workers w ON w.id = r.worker_id
"""
    + LIST_FILTERS
    + " GROUP BY r.state"
)
RUN_NUMBER = re.compile(r"#?([0-9]{1,18})")


def _like(text: str) -> str:
    """``text`` as an ILIKE pattern that matches it anywhere, its own wildcards taken literally."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@router.get("/{project}/runs", response_model=RunList, responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}})
async def list_runs(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    state: Annotated[list[Literal[runs.RUN_STATES]], Query(description="any of these states; repeat it")] = [],  # noqa: B006
    plan_id: Annotated[str | None, Query(pattern=plan_routes.PLAN_ID)] = None,
    step: Annotated[str | None, Query(min_length=1, max_length=STEP_KEY_CHARS, description="a step key")] = None,
    worker_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
    dispatched_by: Annotated[str | None, Query(min_length=1, max_length=100, description="a login")] = None,
    q: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=200,
            description="text in the title, step, plan, repo, branch, worker, login or error, or a run number",
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> RunList:
    """The project's runs, newest first, of the plans the caller may read, with how many are in each state."""
    text = q.strip() if q else None
    number = RUN_NUMBER.fullmatch(text) if text else None
    params = {
        "plan": plan_id,
        "step": step,
        "worker": worker_id,
        "login": dispatched_by,
        "q": _like(text) if text else None,
        "run": int(number[1]) if number else None,
        "states": list(dict.fromkeys(state)),
        "limit": limit,
        "offset": offset,
    }
    async with request.app.state.pool.connection() as conn:
        access = await project_access(conn, user, project)
        params |= {"project": access.project_id, "plans": await visible_plans(conn, access, sink)}
        page = _runs_of(await (await conn.execute(LIST_PAGE, params)).fetchall())
        counts = dict(await (await conn.execute(LIST_COUNTS, params)).fetchall())
    wanted = params["states"] or runs.RUN_STATES
    return RunList(
        runs=page,
        total=sum(counts.get(name, 0) for name in wanted),
        counts=StateCounts(**counts),
        limit=limit,
        offset=offset,
    )


# Run stats by day, for the web's charts

STATS_DAYS = 30  # the days GET .../runs/stats covers without ?days, today in UTC the last of them
MIN_STATS_DAYS, MAX_STATS_DAYS = 7, 90
UTC_TODAY = "SELECT (now() AT TIME ZONE 'UTC')::date"


class RunFigures(BaseModel):
    """The runs that ended in a span of UTC days, of the plans the caller may read."""

    done: int = Field(description="runs that ended done")
    failed: int = Field(description="runs that ended failed")
    lost: int = Field(description="runs whose worker stopped extending the lease")
    cancelled: int = Field(description="runs that ended cancelled, queued or not")
    p50_seconds: float | None = Field(
        description="the median time the runs that ended ran, from started (else leased) to finished, as the runs "
        "pages count it; null when none of them started"
    )
    p90_seconds: float | None = Field(description="the 90th percentile of that time; null when none of them started")
    input_tokens: int = Field(
        description="input tokens not read from the cache, added up from the runs' usage as the run page's usage "
        "card reads it"
    )
    output_tokens: int = Field(description="output tokens, reasoning left out")
    cache_read_tokens: int = Field(description="input tokens read from the cache")
    reasoning_tokens: int = Field(description="reasoning (thinking) tokens")
    runs_with_usage: int = Field(
        description="the runs whose usage the hub could read: a run without usage, or with usage of no shape the "
        "card knows (Claude Code, Codex, opencode), adds no token"
    )


class RunDay(RunFigures):
    day: date = Field(description="a day in UTC")


class RunStats(BaseModel):
    project: str
    days: int = Field(description="the days counted, today in UTC the last")
    first_day: date = Field(description="the oldest day counted, in UTC")
    last_day: date = Field(description="today in UTC")
    by_day: list[RunDay] = Field(
        description="every day counted, the oldest first: a day without a run has zeros and null percentiles"
    )
    total: RunFigures = Field(
        description="the whole span: its counts and tokens are by_day's added up, its percentiles over every run"
    )


# The keys of a run's usage the usage card reads (web/src/components/runs/usage-model.ts), each runtime's own
USAGE_KEYS = (
    *("input_tokens", "cache_read_input_tokens", "output_tokens", "output_tokens_details"),  # Claude Code
    *("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens"),  # Codex, as it streams them
    *("cached_input_tokens", "reasoning_output_tokens"),  # Codex, as its worker reports the run's total
    *("input", "output", "reasoning", "cache_read", "cache"),  # opencode
)


def _usage_number(value: str) -> str:
    """``value`` (a jsonb expression) when it is a positive number, else 0, as the usage card reads a number."""
    return f"CASE WHEN jsonb_typeof({value}) = 'number' THEN greatest(({value})::numeric, 0) ELSE 0 END"


def _usage_either(first: str, second: str) -> str:
    """The number of ``first`` when it holds a value, null or missing being none, else the number of ``second``: the
    card's ``first ?? second``."""
    return f"CASE WHEN {first} IS NOT NULL THEN {_usage_number(first)} ELSE {_usage_number(second)} END"


def _usage_parts() -> dict[str, str]:
    """A run's usage read as the usage card's readTokens reads it, from the keys of USAGE_KEYS taken apart once per
    run (``k``): the shape that reported it, null for none the card knows, then the whole input, the part of it read
    from the cache, the whole output and the reasoning part of it. Codex counts the cache in its input and reasoning in
    its output, Claude Code counts thinking in its output, and opencode keeps the four apart."""

    def key(name: str) -> str:
        return f'k."{name}"'

    def has(*names: str) -> str:
        return " OR ".join(f"jsonb_typeof({key(name)}) = 'number'" for name in names)

    def by_shape(claude: str, codex: str, opencode: str) -> str:
        return (
            f"CASE s.shape WHEN 'claude' THEN {claude} WHEN 'codex' THEN {codex} WHEN 'opencode' THEN {opencode} "
            "ELSE 0 END"
        )

    number, either = _usage_number, _usage_either
    codex = has("inputTokens", "outputTokens", "cachedInputTokens", "cached_input_tokens", "reasoning_output_tokens")
    claude = has("input_tokens", "output_tokens", "cache_read_input_tokens")
    opencode = has("input", "output", "reasoning", "cache_read") + f" OR jsonb_typeof({key('cache')}) = 'object'"
    return {
        "keys": ", ".join(f'"{name}" jsonb' for name in USAGE_KEYS),
        "shape": f"CASE WHEN {codex} THEN 'codex' WHEN {claude} THEN 'claude' WHEN {opencode} THEN 'opencode' END",
        "whole_input": by_shape(
            number(key("input_tokens")), either(key("inputTokens"), key("input_tokens")), number(key("input"))
        ),
        "cached": by_shape(
            number(key("cache_read_input_tokens")),
            either(key("cachedInputTokens"), key("cached_input_tokens")),
            either(key("cache_read"), f"({key('cache')} -> 'read')"),
        ),
        "whole_output": by_shape(
            number(key("output_tokens")), either(key("outputTokens"), key("output_tokens")), number(key("output"))
        ),
        "thought": by_shape(
            number(f"({key('output_tokens_details')} -> 'thinking_tokens')"),
            either(key("reasoningOutputTokens"), key("reasoning_output_tokens")),
            number(key("reasoning")),
        ),
    }


def _run_stats_query() -> str:
    parts = _usage_parts()
    counts = ", ".join(f"count(*) FILTER (WHERE e.state = '{state}')" for state in runs.TERMINAL_STATES)
    return f"""
WITH ended AS (
    SELECT (r.finished_at AT TIME ZONE 'UTC')::date AS day, r.state,
           CASE WHEN coalesce(r.started_at, r.leased_at) IS NOT NULL
                THEN greatest(extract(epoch FROM r.finished_at - coalesce(r.started_at, r.leased_at)), 0)::float8
           END AS seconds,
           s.shape,
           CASE WHEN s.shape = 'codex' THEN least(u.cached, u.whole_input) ELSE u.cached END AS cache_read,
           CASE WHEN s.shape = 'codex' THEN u.whole_input - least(u.cached, u.whole_input) ELSE u.whole_input END
               AS input,
           CASE WHEN s.shape = 'opencode' THEN u.thought ELSE least(u.thought, u.whole_output) END AS reasoning,
           CASE WHEN s.shape = 'opencode' THEN u.whole_output ELSE u.whole_output - least(u.thought, u.whole_output) END
               AS output
      FROM runs r
     CROSS JOIN LATERAL jsonb_to_record(CASE WHEN jsonb_typeof(r.usage) = 'object' THEN r.usage END)
           AS k({parts["keys"]})
     CROSS JOIN LATERAL (SELECT {parts["shape"]} AS shape) s
     CROSS JOIN LATERAL (
         SELECT {parts["whole_input"]} AS whole_input,
                {parts["cached"]} AS cached,
                {parts["whole_output"]} AS whole_output,
                {parts["thought"]} AS thought
     ) u
     WHERE r.project_id = %(project)s AND r.plan_id = ANY(%(plans)s) AND r.state = ANY(%(ended)s)
       AND r.finished_at >= %(since)s AND r.finished_at < %(until)s
)
SELECT e.day, grouping(e.day) = 1, {counts},
       percentile_cont(0.5) WITHIN GROUP (ORDER BY e.seconds), percentile_cont(0.9) WITHIN GROUP (ORDER BY e.seconds),
       sum(e.input), sum(e.output), sum(e.cache_read), sum(e.reasoning), count(e.shape)
  FROM ended e
 GROUP BY ROLLUP (e.day)
"""


RUN_STATS = _run_stats_query()


def _seconds(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _figures(row) -> RunFigures:
    """A row of RUN_STATS after its day and grouping: a sum over no run (null) is 0, a percentile kept to the
    millisecond."""
    *counts, p50, p90, input_tokens, output_tokens, cache_read, reasoning, with_usage = row
    return RunFigures(
        **dict(zip(runs.TERMINAL_STATES, counts, strict=True)),
        p50_seconds=_seconds(p50),
        p90_seconds=_seconds(p90),
        input_tokens=int(input_tokens or 0),
        output_tokens=int(output_tokens or 0),
        cache_read_tokens=int(cache_read or 0),
        reasoning_tokens=int(reasoning or 0),
        runs_with_usage=with_usage,
    )


NO_RUN = _figures([0] * len(runs.TERMINAL_STATES) + [None, None, 0, 0, 0, 0, 0])


@router.get(
    "/{project}/runs/stats",
    response_model=RunStats,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def run_stats(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    days: Annotated[
        int,
        Query(ge=MIN_STATS_DAYS, le=MAX_STATS_DAYS, description="the last days in UTC to count, today included"),
    ] = STATS_DAYS,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> RunStats:
    """The runs of the plans the caller may read that ended on each of the last ``days`` days in UTC: how many in
    each end state, how long they ran and the tokens they used."""
    async with request.app.state.pool.connection() as conn:
        access = await project_access(conn, user, project)
        plans = await visible_plans(conn, access, sink)
        today: date = (await (await conn.execute(UTC_TODAY)).fetchone())[0]
        first = today - timedelta(days=days - 1)
        params = {
            "project": access.project_id,
            "plans": plans,
            "ended": list(runs.TERMINAL_STATES),
            "since": datetime.combine(first, time.min, tzinfo=timezone.utc),
            "until": datetime.combine(today + timedelta(days=1), time.min, tzinfo=timezone.utc),
        }
        rows = await (await conn.execute(RUN_STATS, params)).fetchall()
    by_day, total = {}, NO_RUN
    for day, whole_span, *values in rows:
        if whole_span:
            total = _figures(values)
        else:
            by_day[day] = _figures(values)
    shown = [first + timedelta(days=back) for back in range(days)]
    return RunStats(
        project=access.name,
        days=days,
        first_day=first,
        last_day=today,
        by_day=[RunDay(day=day, **by_day.get(day, NO_RUN).model_dump()) for day in shown],
        total=total,
    )


@router.get(
    "/{project}/runs/{run_id}",
    response_model=Run,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def show_run(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> Run:
    async with request.app.state.pool.connection() as conn:
        await readable_run(conn, user, project, run_id, sink)
        return await run_view(conn, run_id)


# Dispatching


def _dispatcher(access: ProjectAccess) -> None:
    if not has_role(access.role, "writer"):
        held = access.role or "no grant"
        raise HTTPException(
            403, f"dispatching runs in project {access.name} needs the writer role on it; you hold {held}"
        )


PINNABLE = """
SELECT w.owner_id, w.name, w.revoked_at,
       EXISTS (SELECT 1 FROM worker_projects wp WHERE wp.worker_id = w.id AND wp.project_id = %s),
       w.runtimes, w.checkouts, w.agent_version, w.dispatch_from
  FROM workers w WHERE w.id = %s
"""


@dataclass(frozen=True)
class Pinned:
    """The worker a dispatch pins its runs to, as its last heartbeat reported it."""

    id: int
    name: str
    runtimes: dict
    checkouts: dict
    agent_version: str | None


async def _pinnable(conn, user: Principal, access: ProjectAccess, worker_id: int) -> Pinned:
    """403 unless worker ``worker_id`` is ``user``'s own, and unless ``user`` dispatches from a web session when the
    worker takes runs dispatched from the web only; 409 when it is revoked or does not serve the project."""
    row = await (await conn.execute(PINNABLE, (access.project_id, worker_id))).fetchone()
    if row is None or row[0] != user.user_id:  # the same answer for another member's worker and for no worker
        raise HTTPException(
            403, f"a run goes only to a worker of the member who dispatches it, and you have no worker {worker_id}"
        )
    _, name, revoked_at, serves, reported, checkouts, agent_version, dispatch_from = row
    if revoked_at is not None:
        raise HTTPException(409, f"worker {name} was revoked at {revoked_at.isoformat()}; it takes no runs")
    if not serves:
        raise HTTPException(409, f"worker {name} does not take runs of project {access.name}: register it for it")
    if dispatch_from == "web" and user.kind != WEB:
        raise HTTPException(
            403,
            f"worker {name} takes only runs dispatched from a web session, as its owner set it, so a token cannot "
            "hand it work: dispatch on the web, or to another worker; nothing was dispatched",
        )
    return Pinned(worker_id, name, reported or {}, checkouts or {}, agent_version)


def _unfit(worker: Pinned, project: str, kind: str, repos: list[str], runtime: str) -> list[str]:
    """What keeps ``worker`` from ever claiming a run of ``kind`` over ``repos`` asking for ``runtime``, as its last
    heartbeat reported it and the claim checks it; empty when nothing does."""
    problems = []
    if kind == "plan" and not runs.takes_plan_runs(worker.agent_version):
        problems.append(
            f"it runs evo-agents {worker.agent_version or 'of an unknown version'}, and a plan run needs "
            f"{runs.version_text(runs.PLAN_RUN_AGENT)} or later: upgrade it and restart its daemon"
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
    problems = _unfit(worker, access.name, kind, repos, runtime)
    if problems:
        raise HTTPException(
            409,
            f"worker {worker.name} cannot take this run: {'; '.join(problems)}. Dispatch it again once the worker's "
            "heartbeat reports that, or dispatch it to another worker; nothing was dispatched",
        )


INSERT_RUN = """
INSERT INTO runs (project_id, plan_id, step_key, title, plan_revision, dispatched_by, dispatched_via, pinned_worker_id,
                  requested_runtime, runtime, model, mode, approval, timeout_s, parent_run_id, repo, branch)
VALUES (%(project)s, %(plan)s, %(step)s, %(title)s, %(revision)s, %(user)s, %(via)s, %(pinned)s, %(runtime)s,
        %(runtime)s, %(model)s, %(mode)s, %(approval)s, %(timeout)s, %(parent)s, %(repo)s, %(branch)s)
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
        reason = _busy(active[key])
    if reason is not None:
        raise HTTPException(409, f"step {key} of plan {held.plan_id} is not ready: {reason}; nothing was dispatched")
    repo = runs.plan_repo(body, step) or {}
    name = _short(repo.get("repo"))
    if name is None:
        raise HTTPException(
            422, f"step {key} names no repo and the plan does not list exactly one: give the step a repo"
        )
    _fits(pinned, access, "step", [name], runtime)
    params = {
        "project": access.project_id,
        "plan": held.plan_id,
        "step": key,
        "title": runs.step_title(step),
        "revision": held.revision,
        "user": user.user_id,
        "via": dispatch_credential(user.kind),
        "pinned": None if pinned is None else pinned.id,
        "runtime": runtime,
        "model": model,
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


def _run_target(project: str, plan_id: str, key: str | None, run_id: int) -> str:
    """How an audit row names a run: its project, plan and step, or no step for a plan run."""
    return f"{project}/{plan_id}{'' if key is None else f'#{key}'} run:{run_id}"


def _no_plan_run(activity: Activity, plan_id: str) -> None:
    """409 while the plan has an active plan run: its steps are that run's until it ends."""
    if activity.plan_run is not None:
        raise HTTPException(
            409,
            f"{_plan_busy(plan_id, activity.plan_run)}: its steps are that run's until it ends; nothing was dispatched",
        )


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
        pinned = None if body.worker_id is None else await _pinnable(conn, user, access, body.worker_id)
        await _lock_plan(conn, access.project_id, body.plan_id)
        activity = await _activity(conn, access.project_id, body.plan_id)
        _no_plan_run(activity, body.plan_id)
        queued = []
        for key in keys:
            run_id = await _queue_run(
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


INSERT_PLAN_RUN = """
INSERT INTO runs (kind, project_id, plan_id, title, plan_revision, dispatched_by, dispatched_via, pinned_worker_id,
                  requested_runtime, runtime, model, mode, approval, timeout_s, repos)
VALUES ('plan', %(project)s, %(plan)s, %(title)s, %(revision)s, %(user)s, %(via)s, %(pinned)s, %(runtime)s,
        %(runtime)s, %(model)s, %(mode)s, 'auto', %(timeout)s, %(repos)s)
RETURNING id
"""


def _pending(step) -> bool:
    """Whether a step counts as pending: a status of pending or none, or a bare string, which has none."""
    return not isinstance(step, dict) or step.get("status") in (None, "pending")


def _plan_run_repos(held) -> list[dict]:
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


@router.post(
    "/{project}/plan-runs",
    status_code=201,
    response_model=Run,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def dispatch_plan(request: Request, project: ProjectName, body: PlanRunDispatch, user: CurrentUser) -> Run:
    """Queue a plan run: one run, on a worker of the caller's, that does every step of the plan not done yet."""
    async with request.app.state.pool.connection() as conn:
        access = await project_access(conn, user, project)
        _dispatcher(access)
        held = await plan_routes._visible(conn, access, body.plan_id, None)
        pinned = None if body.worker_id is None else await _pinnable(conn, user, access, body.worker_id)
        await _lock_plan(conn, access.project_id, body.plan_id)
        activity = await _activity(conn, access.project_id, body.plan_id)
        if activity.plan_run is not None:
            raise HTTPException(409, f"{_plan_busy(body.plan_id, activity.plan_run)}; nothing was dispatched")
        if activity.steps:
            key, active = min(activity.steps.items(), key=lambda item: item[1].id)
            busy = f"has run #{active.id}, {active.state}, dispatched by {active.dispatched_by}"
            raise HTTPException(
                409,
                f"step {key} of plan {body.plan_id} {busy}: a plan run waits until no run of the plan's steps is "
                "active; nothing was dispatched",
            )
        repos = _plan_run_repos(held)
        _fits(pinned, access, "plan", [entry["repo"] for entry in repos], body.runtime)
        params = {
            "project": access.project_id,
            "plan": held.plan_id,
            "title": runs.step_title(held.body),
            "revision": held.revision,
            "user": user.user_id,
            "via": dispatch_credential(user.kind),
            "pinned": body.worker_id,
            "runtime": body.runtime,
            "model": body.model,
            "mode": body.mode,
            "timeout": body.timeout_h * 3600,
            "repos": Jsonb(repos),
        }
        try:
            async with conn.transaction():
                run_id = (await (await conn.execute(INSERT_PLAN_RUN, params)).fetchone())[0]
        except psycopg.errors.UniqueViolation:  # another plan run of the plan got in first
            busy = f"plan {held.plan_id} has an active plan run already; nothing was dispatched"
            raise HTTPException(409, busy) from None
        except psycopg.errors.CheckViolation:
            raise HTTPException(
                422, f"plan {held.plan_id}: a repo or branch name, or the number of repos, is not one a run can hold"
            ) from None
        await notify_queued(conn, run_id)
        target = _run_target(project, held.plan_id, None, run_id)
        await _audit_run(conn, user, access, audit.RUN_DISPATCH_PLAN, target)
        view = await run_view(conn, run_id)
    log.info(
        "plan run dispatched",
        extra={"project": project, "plan_id": held.plan_id, "run_id": run_id, "repos": len(view.repos or [])},
    )
    return view


# The owner's controls

OWNED_RUN = """
SELECT r.dispatched_by, u.login, r.state, r.plan_id, r.step_key, r.cancel_requested_at, r.requested_runtime, r.mode,
       r.approval, r.timeout_s, r.pinned_worker_id, r.kind, r.model
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


# The worker set to take runs dispatched from the web only that a run is on, or may go to: the one holding it (or that
# parked it), the one it is pinned to, or, for a run queued without a pin and dispatched from the web, one of its
# owner's workers set so, which may claim it.
WEB_ONLY_WORKER = """
SELECT w.name,
       CASE WHEN w.id = r.worker_id AND r.state = 'parked' THEN 'was parked on'
            WHEN w.id = r.worker_id THEN 'is held by'
            WHEN w.id = r.pinned_worker_id THEN 'is pinned to'
            ELSE 'may go to' END
  FROM runs r
  JOIN workers w ON w.dispatch_from = 'web' AND w.revoked_at IS NULL
   AND (w.id = r.worker_id OR w.id = r.pinned_worker_id
        OR (r.state = 'queued' AND r.pinned_worker_id IS NULL AND r.dispatched_via = 'web'
            AND w.owner_id = r.dispatched_by))
 WHERE r.id = %s
 ORDER BY w.id = r.worker_id DESC, w.id = r.pinned_worker_id DESC, w.name
 LIMIT 1
"""


async def web_only_steering(conn, user: Principal, run_id: int, doing: str, instead: str, undone: str) -> None:
    """403 when ``user`` is a token, not a web session, and run ``run_id`` is on, or may go to, a worker whose owner
    set it to take runs dispatched from the web only (``WEB_ONLY_WORKER``): a token that cannot hand such a worker
    work cannot steer the work it has either, by a message to its agent or the answer to a decision, which can resume
    a parked run on it. The refusal says ``doing`` was refused, what to do ``instead``, and that nothing was
    ``undone``."""
    if user.kind == WEB:
        return
    row = await (await conn.execute(WEB_ONLY_WORKER, (run_id,))).fetchone()
    if row is not None:
        name, relation = row
        raise HTTPException(
            403,
            f"run {run_id} {relation} worker {name}, which takes only runs dispatched from a web session, as its owner "
            f"set it, so a token cannot {doing}: {instead}; nothing was {undone}",
        )


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


ASK_TAKEOVER = """
UPDATE runs SET takeover_requested_at = now() WHERE id = %s AND takeover_requested_at IS NULL RETURNING id
"""
ASK_HANDBACK = """
UPDATE runs SET handback_requested_at = now() WHERE id = %s AND handback_requested_at IS NULL RETURNING id
"""


async def _ask(request: Request, project: str, run_id: int, user: Principal, action: str) -> Run:
    """Ask the worker holding run ``run_id`` for a takeover or a handback, which its next heartbeat says."""
    takeover = action == audit.RUN_TAKEOVER
    allowed, verb = (runs.TAKEOVER_STATES, "take over") if takeover else (runs.HANDBACK_STATES, "hand back")
    async with request.app.state.pool.connection() as conn:
        access, row = await _owned_run(conn, user, project, run_id, verb)
        state, plan_id, key = row[2], row[3], row[4]
        if state not in allowed:
            if takeover:
                why = "a person drives it already" if state == "interactive" else "no agent of it runs now"
                needs = "leased or running"
            else:
                why = "it runs headless" if state in runs.TAKEOVER_STATES else "no agent of it runs now"
                needs = "interactive"
            raise HTTPException(409, f"run {run_id} is {state}, so {why}: one may {verb} a run that is {needs}")
        asked = await (await conn.execute(ASK_TAKEOVER if takeover else ASK_HANDBACK, (run_id,))).fetchone()
        if asked is not None:  # an ask repeated while open changes nothing and is not audited again
            await _audit_run(conn, user, access, action, _run_target(project, plan_id, key, run_id))
        view = await run_view(conn, run_id)
    log.info("run control asked", extra={"action": action, "run_id": run_id, "state": view.state, "login": user.login})
    return view


@router.post("/{project}/runs/{run_id}/takeover", response_model=Run, responses=REFUSALS)
async def takeover(request: Request, project: ProjectName, run_id: RunId, user: CurrentUser) -> Run:
    """Ask the worker to stop the agent at the end of its turn and resume its session in a terminal, where a person
    drives it (the run becomes interactive)."""
    return await _ask(request, project, run_id, user, audit.RUN_TAKEOVER)


@router.post("/{project}/runs/{run_id}/handback", response_model=Run, responses=REFUSALS)
async def handback(request: Request, project: ProjectName, run_id: RunId, user: CurrentUser) -> Run:
    """Ask the worker to close the terminal and let the agent go on headless in the same session (the run becomes
    running again)."""
    return await _ask(request, project, run_id, user, audit.RUN_HANDBACK)


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
    """Queue the step of a run that ended again, with the same runtime, model, mode, approval, timeout and worker, at
    the plan's current revision."""
    async with request.app.state.pool.connection() as conn:
        access, row = await _owned_run(conn, user, project, run_id, "rerun")
        _dispatcher(access)
        _, _, state, plan_id, key, _, runtime, mode, approval, timeout_s, pinned, kind, model = row
        if kind == "plan":
            raise HTTPException(
                409, f"run {run_id} is a plan run: dispatch the plan again with POST /v1/projects/{project}/plan-runs"
            )
        if state not in runs.TERMINAL_STATES:
            raise HTTPException(409, f"run {run_id} is still {state}: a run is rerun once it has ended")
        held = await plan_routes._visible(conn, access, plan_id, None)
        worker = None if pinned is None else await _pinnable(conn, user, access, pinned)
        await _lock_plan(conn, access.project_id, plan_id)
        activity = await _activity(conn, access.project_id, plan_id)
        _no_plan_run(activity, plan_id)
        new_id = await _queue_run(
            conn,
            access,
            user,
            held,
            key,
            activity.steps,
            runtime=runtime,
            model=model,
            mode=mode,
            approval=approval,
            timeout_s=timeout_s,
            pinned=worker,
            parent=run_id,
        )
        target = f"{_run_target(project, plan_id, key, new_id)} rerun of run:{run_id}"
        await _audit_run(conn, user, access, audit.RUN_RERUN, target)
        view = await run_view(conn, new_id)
    log.info("run rerun", extra={"run_id": new_id, "rerun_of": run_id, "login": user.login})
    return view


# The worker's side


class RunWakeups:
    """The claims waiting in this api process, woken by RUNS_CHANNEL on the process's one LISTEN (``listen``),
    opened by the first claim or stream. A notification, or a newer claim of the same worker, wakes every waiting
    claim, which then looks at the queue again; a claim also looks every CLAIM_POLL_SECONDS, so it never depends on
    the connection being up."""

    def __init__(self, listener: Listener):
        self._listener = listener
        listener.on(RUNS_CHANNEL, lambda payload: self.wake())
        self._event = asyncio.Event()  # set by the next wake-up, then replaced
        self.generation = 0  # counts wake-ups; a claim waits only while it has not changed
        self._tickets: dict[int, int] = {}  # worker id: the number of its newest claim

    def start(self) -> None:
        self._listener.start()

    def wake(self) -> None:
        self.generation += 1
        self._event.set()
        self._event = asyncio.Event()

    async def wait(self, seen: int, timeout: float) -> None:
        """Return once something woke the claims after generation ``seen``, or after ``timeout`` seconds."""
        if self.generation != seen:
            return
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except asyncio.TimeoutError:  # not TimeoutError itself before Python 3.11
            pass

    async def ticket(self, worker_id: int) -> int:
        """The number of a new claim of ``worker_id``, which makes any older one of it end without a run."""
        number = self._tickets.get(worker_id, 0) + 1
        self._tickets[worker_id] = number
        self.wake()
        return number

    def current(self, worker_id: int, number: int) -> bool:
        return self._tickets.get(worker_id) == number

    def done(self, worker_id: int, number: int) -> None:
        if self._tickets.get(worker_id) == number:
            del self._tickets[worker_id]


WORKER_OF_TOKEN = """
SELECT id, owner_id, name, slots, runtimes, checkouts, drained_at, revoked_at, agent_version, dispatch_from
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
# A run of one step needs a checkout of its repo, and a plan run one of every repo in its repos and a daemon of
# runs.PLAN_RUN_AGENT or later. A worker set to take runs dispatched from the web only passes over the others, those
# dispatched before schema 0011 included.
CLAIMABLE = """
WITH checkouts (project_id, repo) AS (SELECT * FROM unnest(%(pids)s::bigint[], %(repos)s::text[]))
SELECT r.id, r.runtime
  FROM runs r
 WHERE r.state = 'queued' AND r.project_id = ANY(%(projects)s) AND r.dispatched_by = %(owner)s
   AND (r.pinned_worker_id IS NULL OR r.pinned_worker_id = %(worker)s)
   AND (%(dispatch_from)s <> 'web' OR r.dispatched_via = 'web')
   AND (r.runtime = 'any' OR r.runtime = ANY(%(runtimes)s))
   AND (r.kind <> 'plan' OR %(plan_runs)s)
   AND CASE WHEN r.kind = 'plan'
            THEN NOT EXISTS (SELECT 1 FROM jsonb_array_elements(r.repos) AS needed (entry)
                              WHERE NOT EXISTS (SELECT 1 FROM checkouts c WHERE c.project_id = r.project_id
                                                                             AND c.repo = needed.entry ->> 'repo'))
            ELSE EXISTS (SELECT 1 FROM checkouts c WHERE c.project_id = r.project_id AND c.repo = r.repo)
       END
 ORDER BY r.id
 LIMIT 1
   FOR UPDATE OF r SKIP LOCKED
"""
HELD_COUNT = "SELECT count(*) FROM runs WHERE worker_id = %s AND state = ANY(%s)"
REVISION_BODY = "SELECT body FROM plan_revisions WHERE project_id = %s AND plan_id = %s AND revision = %s"
CURRENT_PLAN = "SELECT body, revision FROM plans WHERE project_id = %s AND plan_id = %s"


class ClaimAbandoned(Exception):
    """The worker hung up before its claim was answered; the lease the claim took is rolled back."""

    def __init__(self, run_id: int):
        super().__init__(f"the worker hung up before run {run_id} was handed to it")
        self.run_id = run_id


def lease_of(request: Request) -> timedelta:
    """How long a claim and each heartbeat lease a run for: EVO_HUB_RUN_LEASE_SECONDS."""
    return timedelta(seconds=request.app.state.config.run_lease_seconds)


async def _try_claim(
    pool, user: Principal, lease: timedelta, gone: Callable[[], Awaitable[bool]] | None = None
) -> RunSpec | None:
    """Lease the run the worker of ``user`` may take now, if any, for ``lease`` (see the module's docstring). When
    ``gone`` says the worker hung up once the run is leased, raise ClaimAbandoned before the transaction commits,
    which rolls the lease back."""
    async with pool.connection() as conn:
        worker_id, owner_id, name, slots, reported, checkouts, drained_at, _, version, dispatch_from = await _worker_of(
            conn, user
        )
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
            "plan_runs": runs.takes_plan_runs(version),
            "dispatch_from": dispatch_from,
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
            lease=lease,
        )
        spec = await _run_spec(conn, run_id)
        if gone is not None and await gone():
            raise ClaimAbandoned(run_id)  # leaving the block rolls the transaction back
    log.info("run claimed", extra={"run_id": run_id, "worker_id": worker_id, "runtime": runtime})
    return spec


async def _run_spec(conn, run_id: int) -> RunSpec:
    view = await run_view(conn, run_id)
    project_id = (await (await conn.execute("SELECT project_id FROM runs WHERE id = %s", (run_id,))).fetchone())[0]
    row = await (await conn.execute(REVISION_BODY, (project_id, view.plan_id, view.plan_revision))).fetchone()
    plan = row[0] if row else {"id": view.plan_id, "steps": []}
    copy = None
    if view.kind == "plan":
        current = await (await conn.execute(CURRENT_PLAN, (project_id, view.plan_id))).fetchone()
        body, revision = current if current else (plan, view.plan_revision)  # the plan gone: as dispatched
        copy = PlanCopy(body=body, revision=revision)
        prompt = runs.build_plan_prompt(copy.body, [repo.model_dump() for repo in view.repos or []])
        title = view.title
    else:
        try:
            step = plan["steps"][step_index(plan, view.step_key)]
        except (PlanProblem, KeyError, TypeError):
            step = None
        if not isinstance(step, dict):
            step = {"id": view.step_key}
        prompt = runs.build_prompt(plan, step, {"repo": view.repo, "branch": view.branch})
        title = _text_or_none(step.get("title"))
    return RunSpec(
        id=view.id,
        kind=view.kind,
        project=view.project,
        plan_id=view.plan_id,
        step_key=view.step_key,
        title=title,
        plan_revision=view.plan_revision,
        attempt=view.attempt,
        max_attempts=view.max_attempts,
        parent_run_id=view.parent_run_id,
        resume_of_run_id=view.resume_of_run_id,
        session_id=view.session_id,
        runtime=view.runtime,
        model=view.model,
        mode=view.mode,
        approval=view.approval,
        timeout_min=view.timeout_min,
        repo=view.repo,
        branch=view.branch,
        repos=view.repos,
        lease_expires_at=view.lease_expires_at,
        prompt=prompt,
        plan=copy,
    )


@worker_router.post("/claim", response_model=Claim, responses={403: {"model": ErrorBody}})
async def claim(request: Request, user: CurrentUser, body: ClaimRequest | None = None) -> Claim:
    """Wait up to ``wait_s`` seconds (25 by default) for a run this worker may take, and lease it."""
    wait = (body or ClaimRequest()).wait_s
    wakeups: RunWakeups = request.app.state.run_wakeups
    wakeups.start()
    pool = request.app.state.pool
    lease = lease_of(request)
    async with pool.connection() as conn:
        worker_id = (await _worker_of(conn, user))[0]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    number = await wakeups.ticket(worker_id)
    try:
        while True:
            # Hung up already: a run leased now would wait out its lease with nobody to run it.
            if await request.is_disconnected():
                return Claim(run=None)
            seen = wakeups.generation
            try:
                spec = await _try_claim(pool, user, lease, request.is_disconnected)
            except ClaimAbandoned as exc:
                log.info("claim abandoned; the run stays queued", extra={"run_id": exc.run_id, "worker_id": worker_id})
                wakeups.wake()  # the run is queued again: the other claims waiting here look at it
                return Claim(run=None)
            if spec is not None:
                return Claim(run=spec)
            remaining = deadline - loop.time()
            if remaining <= 0 or not wakeups.current(worker_id, number):
                return Claim(run=None)
            await wakeups.wait(seen, min(remaining, CLAIM_POLL_SECONDS))
    finally:
        wakeups.done(worker_id, number)


EXTEND = f"""
UPDATE runs SET lease_expires_at = now() + %(lease)s, {SETTLE}
 WHERE worker_id = %(worker)s AND id = ANY(%(ids)s) AND state = ANY(%(held)s)
RETURNING id, state, cancel_requested_at IS NOT NULL, lease_expires_at, takeover_requested_at IS NOT NULL,
          handback_requested_at IS NOT NULL
"""
# Runs of the worker it should let go of without cancelling: parked, or done because a new run resumes them, which
# needs their session and worktrees on this worker.
PARKED_HERE = """
SELECT r.id, r.state
  FROM runs r
 WHERE r.worker_id = %s AND r.id = ANY(%s)
   AND (r.state = 'parked' OR (r.state = 'done' AND EXISTS (SELECT 1 FROM runs n WHERE n.resume_of_run_id = r.id)))
"""
INBOX = """
SELECT run_id, count(*) FROM run_inbox WHERE run_id = ANY(%s) AND delivered_at IS NULL GROUP BY run_id
"""
OPEN_DECISIONS = "SELECT run_id, count(*) FROM decisions WHERE run_id = ANY(%s) AND state = 'open' GROUP BY run_id"
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
        worker_id, _, _, _, _, _, drained_at, *_ = await _worker_of(conn, user)
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
        params = {
            "lease": lease_of(request),
            "worker": worker_id,
            "ids": reported,
            "held": list(runs.HELD_STATES),
            "clock": list(runs.CLOCK_STATES),
        }
        extended = {row[0]: row[1:] for row in await (await conn.execute(EXTEND, params)).fetchall()}
        others = [run_id for run_id in reported if run_id not in extended]
        parked = dict(await (await conn.execute(PARKED_HERE, (worker_id, others))).fetchall()) if others else {}
        known = [*extended, *parked]
        waiting = dict(await (await conn.execute(INBOX, (list(extended),))).fetchall()) if extended else {}
        open_decisions = dict(await (await conn.execute(OPEN_DECISIONS, (known,))).fetchall()) if known else {}
    terminals = request.app.state.terminals
    controls = []
    for run_id in reported:
        if run_id in extended:
            state, cancel, lease, takeover, handback = extended[run_id]
            controls.append(
                RunControl(
                    id=run_id,
                    held=True,
                    state=state,
                    lease_expires_at=lease,
                    cancel=cancel,
                    takeover=takeover,
                    handback=handback,
                    terminal_open=terminals.waiting(run_id),
                    inbox=waiting.get(run_id, 0),
                    decisions=open_decisions.get(run_id, 0),
                )
            )
        elif run_id in parked:  # let go of it without cancelling: its session goes on, or will, on this worker
            controls.append(
                RunControl(
                    id=run_id,
                    held=False,
                    state=parked[run_id],
                    lease_expires_at=None,
                    cancel=False,
                    park=True,
                    decisions=open_decisions.get(run_id, 0),
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
REPORTED_RUN = "SELECT state, approval, worker_id, cancel_requested_at, kind FROM runs WHERE id = %s FOR UPDATE"
# What a run may wait for: a decision still open, or an answer the worker has not taken for the agent yet.
WAITS_FOR = """
SELECT EXISTS (SELECT 1 FROM decisions WHERE run_id = %(id)s AND state = 'open')
    OR EXISTS (SELECT 1 FROM run_inbox WHERE run_id = %(id)s AND decision_id IS NOT NULL AND delivered_at IS NULL)
"""


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
        state, approval, _, cancel_requested_at, kind = row
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
        _check_verdict(run_id, kind, approval, body)
        if body.state == "waiting" and not (await (await conn.execute(WAITS_FOR, {"id": run_id})).fetchone())[0]:
            raise HTTPException(
                409,
                f"run {run_id} has no open decision and no answer waiting for the agent: a run waits only for the "
                "answer to a decision of its own, and the time it waits does not count toward its timeout",
            )
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
    if body.state not in runs.HELD_STATES:  # the move gave back the run's leases: revoke its GitHub tokens now
        from evo_agents.hub.server import credentials

        app_state = request.app.state
        await credentials.revoke_tokens(app_state.pool, app_state.sealer, app_state.github_app, run_id=run_id)
    return view


def _check_verdict(run_id: int, kind: str, approval: str, body: StateReport) -> None:
    """409 for a verdict the run's approval, or its kind, does not allow. A plan run ends done without verify
    results, since each of its steps was verified when it was reported, and never waits in review."""
    if kind == "plan":
        if body.state == "review":
            raise HTTPException(409, f"run {run_id} is a plan run: report done or failed, not review")
        if body.state == "done" and any(item.exit_code != 0 for item in body.verify or []):
            raise HTTPException(409, f"run {run_id} is done only when every verify command it reports exited 0")
        return
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


# A plan run's plan and its steps

StepKey = Annotated[str, Path(min_length=1, max_length=STEP_KEY_CHARS, description="the step's id, or its order")]
HELD_PLAN_RUN = """
SELECT r.state, r.worker_id, r.kind, r.project_id, p.name, r.plan_id, r.dispatched_by, u.login, r.repos
  FROM runs r JOIN projects p ON p.id = r.project_id JOIN users u ON u.id = r.dispatched_by
 WHERE r.id = %s
"""


async def _held_plan_run(conn, user: Principal, run_id: int, *, lock: bool = False):
    """(worker name, row of HELD_PLAN_RUN) of a plan run the worker of ``user`` holds, its row locked with ``lock``;
    404 for any other run, a run of one step included."""
    worker_id, _, name, *_ = await _worker_of(conn, user)
    statement = HELD_PLAN_RUN + ("   FOR UPDATE OF r" if lock else "")
    row = await (await conn.execute(statement, (run_id,))).fetchone()
    if row is None or row[1] != worker_id or row[0] not in runs.HELD_STATES:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    if row[2] != "plan":
        raise HTTPException(
            404,
            f"run {run_id} is a run of one step, which has no plan to read or steps to report: report its state with "
            f"POST /v1/worker/runs/{run_id}/state",
        )
    return name, row


async def _dispatcher_access(conn, row, user: Principal) -> ProjectAccess:
    """The access to the run's project of the member who dispatched it, as whom the worker reads and writes the plan."""
    actor = Principal(row[6], row[7], False, user.token_id, MACHINE, "")
    access = await project_access(conn, actor, row[4])
    plan_routes._reader(access)
    return access


@worker_router.get("/runs/{run_id}/plan", response_model=plan_routes.Plan, responses=REFUSALS)
async def read_run_plan(request: Request, run_id: RunId, user: CurrentUser):
    """The plan of a plan run this worker holds, as the hub holds it now, read as the member who dispatched the run."""
    async with request.app.state.pool.connection() as conn:
        _, row = await _held_plan_run(conn, user, run_id)
        access = await _dispatcher_access(conn, row, user)
        held = await plan_routes._visible(conn, access, row[5], None)
    return held.view(row[4])


def _check_step_report(run_id: int, key: str, body: StepReport) -> None:
    """422 for a step reported done without verify results, or with one that exited other than 0."""
    if body.status != "done":
        return
    if not body.verify:
        raise HTTPException(
            422,
            f"run {run_id}: step {key} is done only with the verify commands the worker ran again, each with its exit "
            "code; report them, or report the step pending",
        )
    failed = [item for item in body.verify if item.exit_code != 0]
    if failed:
        shown = "; ".join(f"`{' '.join(item.command.split())}` exited {item.exit_code}" for item in failed)
        raise HTTPException(
            422, f"run {run_id}: step {key} is not done, since {shown}; fix it and verify again, or report it pending"
        )


def _report_repo(run_id: int, body: StepReport, plan: dict, step, repos: list | None) -> dict | None:
    """The run's repo the step was done in: the one the report names (422 when the run has no such repo), else the
    one the plan gives the step, when the run has it."""
    by_name = {entry["repo"]: entry for entry in repos or [] if isinstance(entry, dict)}
    if body.repo is not None:
        if body.repo not in by_name:
            raise HTTPException(422, f"run {run_id} works in {', '.join(by_name) or 'no repo'}, not in {body.repo}")
        return by_name[body.repo]
    name = (runs.plan_repo(plan, step if isinstance(step, dict) else {}) or {}).get("repo")
    return by_name.get(name) if isinstance(name, str) else None


def _step_status(plan: dict, key: str) -> str | None:
    try:
        step = plan["steps"][step_index(plan, key)]
    except (PlanProblem, KeyError, TypeError):
        return None
    return _text_or_none(step.get("status", "pending")) if isinstance(step, dict) else None


@worker_router.post(
    "/runs/{run_id}/steps/{key}", response_model=StepWritten, responses={**REFUSALS, 422: {"model": ErrorBody}}
)
async def report_step(request: Request, run_id: RunId, key: StepKey, body: StepReport, user: CurrentUser):
    """Write a step of the plan of a plan run this worker holds, as the member who dispatched the run."""
    async with request.app.state.pool.connection() as conn:
        _, row = await _held_plan_run(conn, user, run_id, lock=True)
        _, _, _, project_id, project, plan_id, dispatcher_id, _, repos = row
        _check_step_report(run_id, key, body)
        current = await (await conn.execute(CURRENT_PLAN, (project_id, plan_id))).fetchone()
        if current is None:
            raise HTTPException(404, f"plan {plan_id} of run {run_id} is not on the hub any more")
        plan = current[0]
        try:
            index = step_index(plan, key)
        except PlanProblem as exc:
            raise HTTPException(404, f"{exc}: run {run_id} reports only the steps of its plan") from None
        step = plan["steps"][index]
        repo = _report_repo(run_id, body, plan, step, repos) or {}
        verify = [item.model_dump(exclude_none=True) for item in body.verify or []]
        found = replace(
            await run_step(conn, run_id),
            step_key=key,
            repo=repo.get("repo"),
            branch=repo.get("branch"),
            commit_sha=body.commit_sha,
            diffstat=None,
            verify=verify,
        )
        updates = report_updates(found, body.status, body.evidence)

        def updates_for(held_step):
            if held_step.get("status") != "done":
                return updates
            if body.status == "done":  # a resend: the step is done already
                return None
            raise HTTPException(409, f"step {key} of plan {plan_id} is done already, and the hub never sets it back")

        try:
            revision = await write_step(conn, found, updates_for, token_id=user.token_id, strict=True)
        except StepNotWritten as exc:
            raise HTTPException(exc.status, f"step {key} of plan {plan_id} was not written: {exc.message}") from None
        if revision is not None:
            shown = f"step {key}: {body.status}"
            if body.commit_sha and repo:
                shown += f" ({repo['repo']}@{body.commit_sha[:12]})"
            report = {"step": key, "status": body.status, "repo": repo.get("repo"), "commit_sha": body.commit_sha}
            await write_event(conn, run_id, {"text": shown, "step_report": report})
            target = f"{_run_target(project, plan_id, key, run_id)} status={body.status}"
            await audit.record(
                conn,
                actor_id=dispatcher_id,
                token_id=user.token_id,
                action=audit.RUN_STEP_REPORT,
                target=target,
                project_id=project_id,
            )
        after = await (await conn.execute(CURRENT_PLAN, (project_id, plan_id))).fetchone()
    log.info(
        "plan run step reported",
        extra={"run_id": run_id, "step": key, "status": body.status, "written": revision is not None},
    )
    return StepWritten(
        run_id=run_id,
        plan_id=plan_id,
        step_key=key,
        status=_step_status(after[0], key),
        revision=after[1],
        written=revision is not None,
    )
