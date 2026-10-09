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
web session, machine for a token, schedule for a run the night shift of the project's charter queued
(``evo_agents.hub.server.curator``), which also has a budget: its caps, which its claim hands the worker with what it
spent already (``ClaimedBudget``). A worker whose owner set its dispatch_from to web takes runs dispatched from a web
session only: a dispatch, plan run or rerun pinned to it with a token gets 403 saying so, and its claims pass over
the runs dispatched with a token.

A plan the Curator made (``evo_agents.hub.server.changes``) is run by the night shift alone: a dispatch of its steps, a
plan run of it and a rerun of a run of it get 409, as does a rerun of a review run or a judge run, which only the night
shift queues. A judge run is claimed only by a worker whose daemon says it runs judge runs, with a checkout of its repo,
and comes with ``judge.build_judge_prompt``; the claim of any run of the Curator says so (``curator``: its role, the
charter's protected paths, and the change).

POST /v1/projects/{p}/plan-runs (writer) queues a plan run (``runs.RUN_KINDS``): one run, on one worker of the
caller's, that does every step of the plan not done yet. It is refused with 409 when the plan has no pending step,
when the plan has an active run of any kind, or when a step not done names no repo and the plan does not list exactly
one; the run's repos are those of the steps not done, each with the branch the plan's repos name. A dispatch of steps
and a dispatch of a plan run take the same transaction-scoped advisory lock of the plan (``plan_lock_key``) before
they look at its active runs, so the two exclude each other: a step of a plan with an active plan run gets 409 ("plan X
has plan run #N"), and a plan run waits until no run of the plan's steps is active. Each plan run is audited
(run.dispatch_plan). ready-steps names the plan's active plan run (``plan_run``), and no step is ready while it is.

POST /v1/projects/{p}/author-runs (writer) queues an author run (``evo_agents.hub.author``): a plan written from the
caller's request (at most ``author.MAX_REQUEST_BYTES`` of UTF-8, 422 otherwise) with create-exec-plan, on the worker of
the caller's it names (403 for any other id), on claude-code alone (422 for another runtime, saying why). Its repos are
the project's harness (409 for a project registered without one), then each repo of the project that worker has a
checkout of. A dispatch to a worker whose last heartbeat says no checkout of the harness, or a daemon that does not
run author runs, gets 409. It is audited (run.dispatch_author) without its request. A claim takes an author run only
with a checkout of the harness and a daemon that says it runs author runs, and hands it a presigned GET of the latest
version of the global skill create-exec-plan (``RunSpec.skills``); without that skill, or a blob store, the run fails
at the claim, saying so. The author runs of a new plan are listed and read as a review run is, through the project's
default label; one is never rerun (409).

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
time, settles their agent time (``run_state.settle``), and answers with control: per run, whether to cancel (asked by
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
import functools
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    Integer,
    Numeric,
    String,
    Text,
    and_,
    bindparam,
    case,
    cast,
    column,
    exists,
    extract,
    func,
    insert,
    literal,
    or_,
    select,
    true,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, DOUBLE_PRECISION, JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import author, curator, runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.blobs import MAX_GET_TTL
from evo_agents.hub.credentials import DISPATCHED_VIA, dispatch_credential
from evo_agents.hub.db import one_of
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
    StepNotWritten,
    evidence,
    move_run,
    notify_queued,
    report_updates,
    run_step,
    settle,
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
RUN_KIND = r"^[a-z][a-z0-9_]{0,31}$"  # a kind of run a heartbeat names, known to this hub or not
MAX_RUN_KINDS = 20
MAX_STEP_EVIDENCE_CHARS = MAX_EVIDENCE_BYTES  # the agent's evidence of one step; the hub's adds to it, within the bytes

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


class AuthorRunDispatch(BaseModel):
    request: str = Field(
        min_length=1,
        max_length=author.MAX_REQUEST_BYTES,
        description=f"what the plan should achieve, in the member's words: at most {author.MAX_REQUEST_BYTES} bytes "
        "of UTF-8",
    )
    worker_id: int = Field(ge=1, le=MAX_ID, description="the worker of yours the run goes to; an author run is pinned")
    runtime: Literal[REQUESTED_RUNTIMES] = Field(
        "claude-code",
        description="claude-code, the one runtime an author run takes (any means it); another one is 422",
    )
    model: ModelName | None = Field(None, description="the model to use, as Claude Code names it; null: its own choice")
    timeout_h: Literal[author.AUTHOR_TIMEOUT_CHOICES] = Field(
        author.DEFAULT_TIMEOUT_H, description="hours of agent time the run may take"
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


class RunBudget(BaseModel):
    """The caps of a run the night shift queued (``evo_agents.hub.curator``)."""

    max_usd: float | None = Field(None, description="the most it may cost; Claude Code stops there")
    max_turns: int | None = Field(None, description="the most turns its agent may take")
    max_seconds: int | None = Field(None, description="the agent time it may use; a Codex run stops there")


class ClaimedBudget(RunBudget):
    """A run's caps as its worker gets them, with what it spent already when it goes on from a parked run."""

    spent_usd: float = Field(0.0, description="what the agent session it goes on in cost so far")
    spent_seconds: int = Field(0, description="the agent time it used so far")


class RunRepo(BaseModel):
    """A repo of a plan run, with the branch the plan names for it."""

    repo: str
    branch: str | None = Field(None, description="null when the plan names none: the branch checked out")


class Run(BaseModel):
    id: int
    kind: Literal[runs.RUN_KINDS] = Field(
        description="step: one step of the plan; plan: every step not done yet; review: the night's review of the "
        "project by the Curator, on no plan; judge: the Curator's Judge of a change of its plan; author: a plan "
        "written from a member's request, on no plan when it writes a new one"
    )
    project: str
    plan_id: str = Field(
        description="the plan it works on; empty for a review run, which works on none, and an author run of a new plan"
    )
    step_key: str | None = Field(description="null for a plan run")
    title: str | None = Field(description="the step's title when the run was dispatched; the plan's for a plan run")
    plan_revision: int | None = Field(
        description="the plan revision the run was dispatched from; null for a review run"
    )
    dispatched_by: str = Field(description="the login of the member who dispatched it, its owner")
    dispatched_via: Literal[DISPATCHED_VIA] | None = Field(
        description="the credential it was dispatched with: web, a web session; machine, a token (the command line, "
        "an agent); schedule, the night shift of the project's charter, for its owner; null for a run dispatched "
        "before 0.5.0"
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
    budget: RunBudget | None = Field(None, description="the caps of a run the night shift queued; null for any other")
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
    request: str | None = Field(None, description="the member's request of an author run; null for any other kind")


class ClaimRequest(BaseModel):
    wait_s: float = Field(
        runs.CLAIM_WAIT_SECONDS, ge=0, le=runs.CLAIM_WAIT_SECONDS, description="how long to wait for a run"
    )


class PlanCopy(BaseModel):
    """A plan as the hub holds it at one revision."""

    revision: int
    body: dict


class CuratorSpec(BaseModel):
    """What the worker of a run of the Curator (``evo_agents.hub.server.changes``) is told: the run's role, the
    charter's protected paths its watchdog compares the worktrees with, and for a Builder or a Judge the change, its
    branch, its forge and its pull request; for a Judge the commit to judge."""

    role: Literal["reviewer", "builder", "judge"]
    protected_paths: list[str] = Field(default_factory=list, description="globs of the charter, repo:glob for one repo")
    change_id: int | None = None
    branch: str | None = Field(None, description="curator/..., the one branch a Builder pushes")
    forge: Literal["github", "gitlab"] | None = Field(None, description="gitlab: the push opens the merge request")
    base_branch: str | None = Field(None, description="the default branch the pull request goes into, when known")
    head_sha: str | None = Field(None, description="a Judge's commit to judge; null: the branch's tip")
    pr_url: str | None = None
    judge_key: str | None = Field(
        None,
        description="a judge run's own key, for GET .../judge and POST .../verdict (X-Evo-Judge-Key); the daemon keeps "
        "it in memory alone, never in a file, an environment or a log line",
    )


class RunSkill(BaseModel):
    """A skill the worker writes for the run's agent, as the hub holds it when the run is claimed: an author run's
    create-exec-plan, global, at its latest version."""

    name: str
    scope: Literal["global"] = "global"
    version: int
    sha256: str = Field(description="what the downloaded bytes must hash to")
    size: int
    url: str = Field(description="presigned GET of the bundle; a bearer credential until it expires")
    expires_at: datetime


class RunSpec(BaseModel):
    id: int
    kind: Literal[runs.RUN_KINDS]
    project: str
    plan_id: str = Field(description="empty for a review run")
    step_key: str | None = Field(description="null for a plan run")
    title: str | None
    plan_revision: int | None = Field(
        description="the plan revision the run was dispatched from; null for a review run"
    )
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
    repos: list[RunRepo] | None = Field(
        description="a plan run's or a review run's repos, each with a checkout on this worker"
    )
    lease_expires_at: datetime
    prompt: str
    plan: PlanCopy | None = Field(
        description=f"a plan run's plan at the hub's current revision, for {runs.PLAN_FILE}; null for a run of one step"
    )
    budget: ClaimedBudget | None = Field(None, description="the caps of a run the night shift queued; null otherwise")
    curator: CuratorSpec | None = Field(
        None, description="a run of the Curator: its role and what its worker checks; null for any other run"
    )
    skills: list[RunSkill] = Field(
        default_factory=list,
        description=f"the skills the worker writes under {author.SKILLS_DIR} of the run's directory for its agent: an "
        "author run's create-exec-plan; empty for any other run",
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
    run_kinds: list[Annotated[str, Field(pattern=RUN_KIND)]] | None = Field(
        None,
        max_length=MAX_RUN_KINDS,
        description="the kinds of run the daemon runs; one that says none takes no review run. A kind this hub does "
        "not know is left out",
    )

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


def _run_select():
    """The fields of Run, each column labelled as its field: a run with the name of its project, the login of its
    owner and the name of the worker that claimed it."""
    r, p, u, w = tables.runs, tables.projects, tables.users, tables.workers
    return (
        select(
            r.c.id,
            r.c.kind,
            p.c.name.label("project"),
            func.coalesce(r.c.plan_id, "").label("plan_id"),
            r.c.step_key,
            r.c.title,
            r.c.plan_revision,
            u.c.login.label("dispatched_by"),
            r.c.dispatched_via,
            r.c.worker_id,
            w.c.name.label("worker"),
            r.c.pinned_worker_id,
            r.c.requested_runtime,
            r.c.runtime,
            r.c.model,
            r.c.mode,
            r.c.approval,
            (r.c.timeout_s // 60).label("timeout_min"),
            r.c.run_seconds,
            r.c.attempt,
            r.c.max_attempts,
            r.c.parent_run_id,
            r.c.resume_of_run_id,
            r.c.state,
            r.c.lease_expires_at,
            r.c.session_id,
            r.c.repo,
            r.c.branch,
            r.c.repos,
            r.c.commit_sha,
            r.c.diffstat,
            r.c.verify,
            r.c.evidence,
            r.c.usage,
            r.c.budget,
            r.c.error,
            r.c.log_sha256,
            r.c.diff_sha256,
            r.c.event_seq.label("last_seq"),
            r.c.cancel_requested_at,
            r.c.takeover_requested_at,
            r.c.handback_requested_at,
            r.c.queued_at,
            r.c.leased_at,
            r.c.started_at,
            r.c.waiting_since,
            r.c.parked_at,
            r.c.finished_at,
            r.c.request,
        )
        .join_from(r, p, p.c.id == r.c.project_id)
        .join(u, u.c.id == r.c.dispatched_by)
        .outerjoin(w, w.c.id == r.c.worker_id)
    )


def _runs_of(rows) -> list[Run]:
    return [Run(**row._mapping) for row in rows]


async def run_views(conn: AsyncConnection, run_ids: list[int]) -> list[Run]:
    r = tables.runs
    return _runs_of(await conn.execute(_run_select().where(r.c.id.in_(list(run_ids))).order_by(r.c.id)))


async def run_view(conn: AsyncConnection, run_id: int) -> Run:
    (found,) = await run_views(conn, [run_id])
    return found


@dataclass
class Activity:
    """The active runs of a plan: those of its steps by step key, and its plan run."""

    steps: dict[str, ActiveRun]
    plan_run: ActiveRun | None


async def _activity(conn: AsyncConnection, project_id: int, plan_id: str) -> Activity:
    r, u = tables.runs, tables.users
    query = (
        select(r.c.kind, r.c.step_key, r.c.id, r.c.state, u.c.login)
        .join_from(r, u, u.c.id == r.c.dispatched_by)
        .where(r.c.project_id == project_id, r.c.plan_id == plan_id, r.c.state.in_(runs.ACTIVE_STATES))
        .order_by(r.c.id)
    )
    activity = Activity(steps={}, plan_run=None)
    for row in await conn.execute(query):
        active = ActiveRun(id=row.id, state=row.state, dispatched_by=row.login)
        if row.kind == "plan":
            activity.plan_run = active
        else:
            activity.steps[row.step_key] = active
    return activity


def plan_lock_key(project_id: int, plan_id: str) -> str:
    """The text whose hash names the advisory lock a dispatch of a plan's steps or of its plan run takes."""
    return f"evo-runs:{project_id}:{plan_id}"


async def _lock_plan(conn: AsyncConnection, project_id: int, plan_id: str) -> None:
    """Take the plan's dispatch lock until the caller's transaction ends: a dispatch of its steps and one of its plan
    run then each see the other's run, never both none."""
    key = func.hashtextextended(plan_lock_key(project_id, plan_id), 0)
    await conn.execute(select(func.pg_advisory_xact_lock(key)))


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
    async with request.app.state.engine.begin() as conn:
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


_PLANS_OF = select(tables.plans.c.plan_id, tables.plans.c.label).where(
    tables.plans.c.project_id == bindparam("project_id")
)  # built once: every read of runs or decisions runs it


async def visible_plans(conn: AsyncConnection, access: ProjectAccess, sink: str | None) -> list[str]:
    """The plans of the project whose runs the caller may read: those it may read through ``sink`` (the project's
    hub sink when None), by the plan's label. A run of a plan no longer on the hub is shown to nobody."""
    plan_routes._reader(access)
    through = plan_routes._sink(access, sink)
    rows = await conn.execute(_PLANS_OF, {"project_id": access.project_id})
    return [row.plan_id for row in rows if access.visible(row.label, through)]


async def readable_run(
    conn: AsyncConnection, user: Principal, project: str, run_id: int, sink: str | None
) -> ProjectAccess:
    """The caller's access to ``project`` when it may read run ``run_id`` of it: a grant on the project (404 without,
    403 for a hub admin without one) and the run's plan visible to it (404 otherwise, as for no run). A review run,
    which has no plan, reads as what it reads: the project, through the project's default label; so does an author run
    of a new plan."""
    access = await project_access(conn, user, project)
    plan_routes._reader(access)
    r = tables.runs
    found = select(r.c.plan_id, r.c.kind).where(r.c.id == run_id, r.c.project_id == access.project_id)
    row = (await conn.execute(found)).one_or_none()
    if row is not None and row.plan_id is None:  # a review run, or an author run of a new plan
        visible = _sees_unplanned(access, sink)
    else:
        visible = row is not None and row.plan_id in await visible_plans(conn, access, sink)
    if not visible:
        raise HTTPException(404, f"project {project} has no run {run_id}: see GET /v1/projects/{project}/runs")
    return access


def _sees_unplanned(access: ProjectAccess, sink: str | None) -> bool:
    """Whether the caller reads the runs of the project on no plan: through ``sink``, the project's default label."""
    return access.visible(access.rules.default_label, plan_routes._sink(access, sink))


RUN_NUMBER = re.compile(r"#?([0-9]{1,18})")


def _like(text: str) -> str:
    """``text`` as an ILIKE pattern that matches it anywhere, its own wildcards taken literally."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _list_conditions(*, plan_id: bool, step: bool, worker_id: bool, login: bool, text: bool, number: bool) -> list:
    """The filters of GET .../runs but the state's, which the counts by state leave out: the runs of project
    :project_id and of the plans in :plans, and its author runs of a new plan when :unplanned, then each filter given,
    by its bind parameter (one not given leaves every run): :plan_id, :step, :worker_id, :login, and the text as the
    ILIKE :pattern, or as the run :number."""
    r, u, w = tables.runs, tables.users, tables.workers
    unplanned = and_(r.c.kind == "author", r.c.plan_id.is_(None), bindparam("unplanned", type_=Boolean))
    found = [r.c.project_id == bindparam("project_id"), or_(one_of(r.c.plan_id, name="plans"), unplanned)]
    if plan_id:
        found.append(r.c.plan_id == bindparam("plan_id"))
    if step:
        found.append(r.c.step_key == bindparam("step"))
    if worker_id:
        found.append(r.c.worker_id == bindparam("worker_id"))
    if login:
        found.append(func.lower(u.c.login) == func.lower(bindparam("login", type_=String)))
    if text:
        pattern = bindparam("pattern")
        searched = (r.c.title, r.c.step_key, r.c.plan_id, r.c.repo, r.c.branch, w.c.name, u.c.login, r.c.error)
        matches = [searched_column.ilike(pattern) for searched_column in searched]
        if number:
            matches.append(r.c.id == bindparam("number"))
        found.append(or_(*matches))
    return found


@functools.cache
def _list_statements(states: bool, **shape: bool):
    """The page and the counts by state of GET .../runs for the filters ``shape`` names, built once per shape: a
    page takes :limit and :offset, and :states when ``states``."""
    r, u, w = tables.runs, tables.users, tables.workers
    conditions = _list_conditions(**shape)
    page = _run_select().where(*conditions)
    if states:
        page = page.where(one_of(r.c.state, name="states"))
    limit, offset = bindparam("limit", type_=Integer), bindparam("offset", type_=Integer)
    page = page.order_by(r.c.id.desc()).limit(limit).offset(offset)
    counts = (
        select(r.c.state, func.count().label("runs"))
        .join_from(r, u, u.c.id == r.c.dispatched_by)
        .outerjoin(w, w.c.id == r.c.worker_id)
        .where(*conditions)
        .group_by(r.c.state)
    )
    return page, counts


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
    filters = {"plan_id": plan_id, "step": step, "worker_id": worker_id, "login": dispatched_by}
    states = list(dict.fromkeys(state))
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        plans = await visible_plans(conn, access, sink)
        text = q.strip() if q else None
        number = RUN_NUMBER.fullmatch(text) if text else None
        shape = {name: value is not None for name, value in filters.items()}
        page_query, counts_query = _list_statements(bool(states), **shape, text=bool(text), number=bool(number))
        bound = {
            **filters,
            "project_id": access.project_id,
            "plans": plans,
            "unplanned": _sees_unplanned(access, sink),
            "pattern": _like(text) if text else None,
            "number": int(number[1]) if number else None,
            "states": states,
            "limit": limit,
            "offset": offset,
        }
        page = _runs_of(await conn.execute(page_query, bound))
        counts = {row.state: row.runs for row in await conn.execute(counts_query, bound)}
    wanted = states or runs.RUN_STATES
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


def _usage_number(value):
    """``value`` (a jsonb expression) when it is a positive number, else 0, as the usage card reads a number."""
    return case((func.jsonb_typeof(value) == "number", func.greatest(cast(value, Numeric), 0)), else_=0)


def _usage_either(first, second):
    """The number of ``first`` when it holds a value, null or missing being none, else the number of ``second``: the
    card's ``first ?? second``."""
    return case((first.is_not(None), _usage_number(first)), else_=_usage_number(second))


def _usage_keys(usage):
    """The keys of USAGE_KEYS taken apart once from a run's ``usage`` (``k``), each as jsonb: null for a key it does
    not have, and for usage that is not a JSON object."""
    whole = case((func.jsonb_typeof(usage) == "object", usage))
    keys = func.jsonb_to_record(whole).table_valued(*(column(name, JSONB) for name in USAGE_KEYS))
    return keys.render_derived(name="k", with_types=True).lateral("k")


def _usage_shape(k):
    """The shape that reported the usage of ``k``: codex, claude or opencode, null for none the card knows."""

    def has(*names: str):
        return or_(*(func.jsonb_typeof(k.c[name]) == "number" for name in names))

    codex = has("inputTokens", "outputTokens", "cachedInputTokens", "cached_input_tokens", "reasoning_output_tokens")
    claude = has("input_tokens", "output_tokens", "cache_read_input_tokens")
    opencode = or_(has("input", "output", "reasoning", "cache_read"), func.jsonb_typeof(k.c.cache) == "object")
    return case((codex, "codex"), (claude, "claude"), (opencode, "opencode"))


def _usage_parts(k, shape) -> dict:
    """A run's usage read as the usage card's readTokens reads it, from the keys of ``k`` by its ``shape``: the whole
    input, the part of it read from the cache, the whole output and the reasoning part of it. Codex counts the cache
    in its input and reasoning in its output, Claude Code counts thinking in its output, and opencode keeps the four
    apart."""

    def by_shape(claude, codex, opencode):
        return case({"claude": claude, "codex": codex, "opencode": opencode}, value=shape, else_=0)

    number, either = _usage_number, _usage_either
    return {
        "whole_input": by_shape(number(k.c.input_tokens), either(k.c.inputTokens, k.c.input_tokens), number(k.c.input)),
        "cached": by_shape(
            number(k.c.cache_read_input_tokens),
            either(k.c.cachedInputTokens, k.c.cached_input_tokens),
            either(k.c.cache_read, k.c.cache["read"]),
        ),
        "whole_output": by_shape(
            number(k.c.output_tokens), either(k.c.outputTokens, k.c.output_tokens), number(k.c.output)
        ),
        "thought": by_shape(
            number(k.c.output_tokens_details["thinking_tokens"]),
            either(k.c.reasoningOutputTokens, k.c.reasoning_output_tokens),
            number(k.c.reasoning),
        ),
    }


def _run_stats(project_id: int, plans: list[str], since: datetime, until: datetime):
    """The runs of ``plans`` that ended in [since, until), by UTC day and over the whole span (the row whose
    whole_span is true): how many in each end state, the percentiles of how long they ran and their tokens. Each run's
    usage is taken apart once (``_usage_keys``), its shape read once (``s``) and its four numbers once (``u``)."""
    r = tables.runs
    k = _usage_keys(r.c.usage)
    s = select(_usage_shape(k).label("shape")).correlate(k).lateral("s")
    u = select(*(part.label(name) for name, part in _usage_parts(k, s.c.shape).items())).correlate(k, s).lateral("u")
    began = func.coalesce(r.c.started_at, r.c.leased_at)
    ran = func.greatest(extract("epoch", r.c.finished_at - began), 0)
    codex, opencode = s.c.shape == "codex", s.c.shape == "opencode"
    cache_read = func.least(u.c.cached, u.c.whole_input)
    reasoning = func.least(u.c.thought, u.c.whole_output)
    ended = (
        select(
            cast(func.timezone("UTC", r.c.finished_at), Date).label("day"),
            r.c.state,
            case((began.is_not(None), cast(ran, DOUBLE_PRECISION))).label("seconds"),
            s.c.shape,
            case((codex, cache_read), else_=u.c.cached).label("cache_read"),
            case((codex, u.c.whole_input - cache_read), else_=u.c.whole_input).label("input"),
            case((opencode, u.c.thought), else_=reasoning).label("reasoning"),
            case((opencode, u.c.whole_output), else_=u.c.whole_output - reasoning).label("output"),
        )
        .select_from(r)
        .join(k, true())
        .join(s, true())
        .join(u, true())
        .where(
            r.c.project_id == project_id,
            r.c.plan_id.in_(plans),
            r.c.state.in_(runs.TERMINAL_STATES),
            r.c.finished_at >= since,
            r.c.finished_at < until,
        )
        .cte("ended")
    )
    e = ended.c
    return select(
        e.day,
        (func.grouping(e.day) == 1).label("whole_span"),
        *(func.count().filter(e.state == state).label(state) for state in runs.TERMINAL_STATES),
        func.percentile_cont(0.5).within_group(e.seconds).label("p50_seconds"),
        func.percentile_cont(0.9).within_group(e.seconds).label("p90_seconds"),
        func.sum(e.input).label("input_tokens"),
        func.sum(e.output).label("output_tokens"),
        func.sum(e.cache_read).label("cache_read_tokens"),
        func.sum(e.reasoning).label("reasoning_tokens"),
        func.count(e.shape).label("runs_with_usage"),
    ).group_by(func.rollup(e.day))


def _seconds(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _figures(row) -> RunFigures:
    """A row of ``_run_stats``, by column name: a sum over no run (null) is 0, a percentile kept to the
    millisecond."""
    return RunFigures(
        **{state: row[state] for state in runs.TERMINAL_STATES},
        p50_seconds=_seconds(row["p50_seconds"]),
        p90_seconds=_seconds(row["p90_seconds"]),
        input_tokens=int(row["input_tokens"] or 0),
        output_tokens=int(row["output_tokens"] or 0),
        cache_read_tokens=int(row["cache_read_tokens"] or 0),
        reasoning_tokens=int(row["reasoning_tokens"] or 0),
        runs_with_usage=row["runs_with_usage"],
    )


NO_RUN = _figures(
    dict.fromkeys(runs.TERMINAL_STATES, 0)
    | dict.fromkeys(("input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens", "runs_with_usage"), 0)
    | {"p50_seconds": None, "p90_seconds": None}
)


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
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        plans = await visible_plans(conn, access, sink)
        today: date = (await conn.execute(select(cast(func.timezone("UTC", func.now()), Date)))).scalar_one()
        first = today - timedelta(days=days - 1)
        since = datetime.combine(first, time.min, tzinfo=UTC)
        until = datetime.combine(today + timedelta(days=1), time.min, tzinfo=UTC)
        rows = (await conn.execute(_run_stats(access.project_id, plans, since, until))).all()
    by_day, total = {}, NO_RUN
    for row in rows:
        if row.whole_span:
            total = _figures(row._mapping)
        else:
            by_day[row.day] = _figures(row._mapping)
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
    async with request.app.state.engine.begin() as conn:
        await readable_run(conn, user, project, run_id, sink)
        return await run_view(conn, run_id)


# Dispatching


def _dispatcher(access: ProjectAccess) -> None:
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


async def _pinnable(conn: AsyncConnection, user: Principal, access: ProjectAccess, worker_id: int) -> Pinned:
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


def _unfit(worker: Pinned, project: str, kind: str, repos: list[str], runtime: str) -> list[str]:
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
    problems = _unfit(worker, access.name, kind, repos, runtime)
    if problems:
        raise HTTPException(
            409,
            f"worker {worker.name} cannot take this run: {'; '.join(problems)}. Dispatch it again once the worker's "
            "heartbeat reports that, or dispatch it to another worker; nothing was dispatched",
        )


def _short(value) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


async def _insert_run(conn: AsyncConnection, values: dict) -> int:
    """Insert a run with the column ``values`` in a savepoint and return its id. When the database refuses the row,
    the savepoint is rolled back and the IntegrityError, with the psycopg error that says why in ``orig``, goes on to
    the caller, whose transaction stays usable."""
    r = tables.runs
    async with conn.begin_nested():
        return (await conn.execute(insert(r).values(**values).returning(r.c.id))).scalar_one()


async def _queue_run(
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
        run_id = await _insert_run(conn, values)
    except IntegrityError as exc:
        if isinstance(exc.orig, psycopg.errors.UniqueViolation):  # another dispatch of the step got in first
            raise HTTPException(409, f"step {key} of plan {held.plan_id} has an active run already") from None
        if isinstance(exc.orig, psycopg.errors.CheckViolation):
            raise HTTPException(422, f"step {key}: its repo or branch name is not one a run can hold") from None
        raise
    active[key] = ActiveRun(id=run_id, state="queued", dispatched_by=user.login)
    await notify_queued(conn, run_id)
    return run_id


def _run_target(project: str, plan_id: str | None, key: str | None, run_id: int) -> str:
    """How an audit row names a run: its project, plan and step, or no step for a plan run; a review run, which has
    no plan, as the project's review."""
    if not plan_id:
        return f"{project}/review run:{run_id}"
    return f"{project}/{plan_id}{'' if key is None else f'#{key}'} run:{run_id}"


async def _refuse_curator_plan(conn: AsyncConnection, access: ProjectAccess, plan_id: str) -> None:
    """409 for a plan the Curator made: the night shift alone runs it (``changes.refuse_manual_dispatch``)."""
    from evo_agents.hub.server.changes import refuse_manual_dispatch  # it queues runs through this module

    await refuse_manual_dispatch(conn, access.project_id, plan_id)


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
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _dispatcher(access)
        held = await plan_routes._visible(conn, access, body.plan_id, None)
        await _refuse_curator_plan(conn, access, body.plan_id)
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
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _dispatcher(access)
        held = await plan_routes._visible(conn, access, body.plan_id, None)
        await _refuse_curator_plan(conn, access, body.plan_id)
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
            run_id = await _insert_run(conn, values)
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
        target = _run_target(project, held.plan_id, None, run_id)
        await _audit_run(conn, user, access, audit.RUN_DISPATCH_PLAN, target)
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


@router.post(
    "/{project}/author-runs",
    status_code=201,
    response_model=Run,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def dispatch_author(request: Request, project: ProjectName, body: AuthorRunDispatch, user: CurrentUser) -> Run:
    """Queue an author run: a plan written from the caller's request, with create-exec-plan, on a worker of the
    caller's (``evo_agents.hub.author``)."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _dispatcher(access)
        problem = author.request_problem(body.request) or author.runtime_problem(body.runtime)
        if problem:
            raise HTTPException(422, f"{problem}; nothing was dispatched")
        runtime = author.AUTHOR_RUNTIMES[0] if body.runtime == "any" else body.runtime
        pinned = await _pinnable(conn, user, access, body.worker_id)
        repos = await _author_repos(conn, access, pinned)
        _fits(pinned, access, "author", [repos[0]["repo"]], runtime)
        values = {
            "kind": "author",
            "project_id": access.project_id,
            "plan_id": None,
            "title": author.author_title(body.request),
            "plan_revision": None,
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
            run_id = await _insert_run(conn, values)
        except IntegrityError as exc:
            if isinstance(exc.orig, psycopg.errors.CheckViolation):
                raise HTTPException(
                    422, "a repo name, or the number of repos, is not one a run can hold; nothing was dispatched"
                ) from None
            raise
        await notify_queued(conn, run_id)
        await _audit_run(conn, user, access, audit.RUN_DISPATCH_AUTHOR, f"{project}/author run:{run_id}")
        view = await run_view(conn, run_id)
    log.info(
        "author run dispatched",
        extra={"project": project, "run_id": run_id, "worker_id": pinned.id, "repos": len(repos)},
    )
    return view


# The owner's controls


async def _owned_run(conn: AsyncConnection, user: Principal, project: str, run_id: int, action: str):
    """(access, row) for a run of ``project`` that ``user`` dispatched, its row locked: dispatched_by, login, state,
    plan_id, step_key, cancel_requested_at, requested_runtime, mode, approval, timeout_s, pinned_worker_id, kind and
    model. 404 for a run ``user`` cannot see, 403 for another member's."""
    access = await project_access(conn, user, project)
    plan_routes._reader(access)
    r, u = tables.runs, tables.users
    query = (
        select(
            r.c.dispatched_by,
            u.c.login,
            r.c.state,
            r.c.plan_id,
            r.c.step_key,
            r.c.cancel_requested_at,
            r.c.requested_runtime,
            r.c.mode,
            r.c.approval,
            r.c.timeout_s,
            r.c.pinned_worker_id,
            r.c.kind,
            r.c.model,
        )
        .join_from(r, u, u.c.id == r.c.dispatched_by)
        .where(r.c.id == run_id, r.c.project_id == access.project_id)
        .with_for_update(of=r)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        raise HTTPException(404, f"project {project} has no run {run_id}: see GET /v1/projects/{project}/runs")
    if row[0] != user.user_id:
        raise HTTPException(403, f"only {row[1]}, who dispatched run {run_id}, may {action} it")
    return access, row


def _web_only_worker(run_id: int):
    """The worker set to take runs dispatched from the web only that run ``run_id`` is on, or may go to, with how:
    the one holding it (or that parked it), the one it is pinned to, or, for a run queued without a pin and
    dispatched from the web, one of its owner's workers set so, which may claim it; in that order, then by name."""
    r, w = tables.runs, tables.workers
    held, pinned = w.c.id == r.c.worker_id, w.c.id == r.c.pinned_worker_id
    claimable = and_(
        r.c.state == "queued",
        r.c.pinned_worker_id.is_(None),
        r.c.dispatched_via == "web",
        w.c.owner_id == r.c.dispatched_by,
    )
    relation = case(
        (and_(held, r.c.state == "parked"), "was parked on"),
        (held, "is held by"),
        (pinned, "is pinned to"),
        else_="may go to",
    )
    return (
        select(w.c.name, relation.label("relation"))
        .join_from(r, w, and_(w.c.dispatch_from == "web", w.c.revoked_at.is_(None), or_(held, pinned, claimable)))
        .where(r.c.id == run_id)
        .order_by(held.desc(), pinned.desc(), w.c.name)
        .limit(1)
    )


async def web_only_steering(
    conn: AsyncConnection, user: Principal, run_id: int, doing: str, instead: str, undone: str
) -> None:
    """403 when ``user`` is a token, not a web session, and run ``run_id`` is on, or may go to, a worker whose owner
    set it to take runs dispatched from the web only (``_web_only_worker``): a token that cannot hand such a worker
    work cannot steer the work it has either, by a message to its agent or the answer to a decision, which can resume
    a parked run on it. The refusal says ``doing`` was refused, what to do ``instead``, and that nothing was
    ``undone``."""
    if user.kind == WEB:
        return
    row = (await conn.execute(_web_only_worker(run_id))).one_or_none()
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
    async with request.app.state.engine.begin() as conn:
        access, row = await _owned_run(conn, user, project, run_id, "cancel")
        state, plan_id, key, cancel_requested_at = row[2], row[3], row[4], row[5]
        if state in runs.TERMINAL_STATES:
            raise HTTPException(409, f"run {run_id} is {state}, which is final")
        if state in runs.HELD_STATES:
            if cancel_requested_at is not None:  # asked already: nothing changes, nothing is audited
                return await run_view(conn, run_id)
            r = tables.runs
            await conn.execute(update(r).values(cancel_requested_at=func.now()).where(r.c.id == run_id))
        else:
            reason = f"{user.login} cancelled it"
            await move_run(conn, run_id, state, "cancelled", "owner", reason=reason, token_id=user.token_id)
        await _audit_run(conn, user, access, audit.RUN_CANCEL, _run_target(project, plan_id, key, run_id))
        view = await run_view(conn, run_id)
    log.info("run cancelled", extra={"run_id": run_id, "state": view.state, "login": user.login})
    return view


async def _asked(conn: AsyncConnection, run_id: int, asked_at) -> bool:
    """Set ``asked_at``, a column of runs, to now unless it is set already: True when this set it."""
    r = tables.runs
    ask = update(r).values({asked_at: func.now()}).where(r.c.id == run_id, asked_at.is_(None)).returning(r.c.id)
    return (await conn.execute(ask)).one_or_none() is not None


async def ask_takeover(conn: AsyncConnection, run_id: int) -> bool:
    """Ask the worker holding run ``run_id`` for a takeover; False when one was asked already and is still open."""
    return await _asked(conn, run_id, tables.runs.c.takeover_requested_at)


async def ask_handback(conn: AsyncConnection, run_id: int) -> bool:
    """Ask the worker holding run ``run_id`` for a handback; False when one was asked already and is still open."""
    return await _asked(conn, run_id, tables.runs.c.handback_requested_at)


async def _ask(request: Request, project: str, run_id: int, user: Principal, action: str) -> Run:
    """Ask the worker holding run ``run_id`` for a takeover or a handback, which its next heartbeat says."""
    takeover = action == audit.RUN_TAKEOVER
    allowed, verb = (runs.TAKEOVER_STATES, "take over") if takeover else (runs.HANDBACK_STATES, "hand back")
    async with request.app.state.engine.begin() as conn:
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
        asked = await (ask_takeover if takeover else ask_handback)(conn, run_id)
        if asked:  # an ask repeated while open changes nothing and is not audited again
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
    async with request.app.state.engine.begin() as conn:
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
    async with request.app.state.engine.begin() as conn:
        access, row = await _owned_run(conn, user, project, run_id, "rerun")
        _dispatcher(access)
        _, _, state, plan_id, key, _, runtime, mode, approval, timeout_s, pinned, kind, model = row
        if kind == "plan":
            raise HTTPException(
                409, f"run {run_id} is a plan run: dispatch the plan again with POST /v1/projects/{project}/plan-runs"
            )
        if kind in runs.CURATOR_KINDS:
            raise HTTPException(
                409, f"run {run_id} is a {kind} run, which only the night shift of the project's charter queues"
            )
        if kind == "author":
            raise HTTPException(
                409, f"run {run_id} is an author run: dispatch a new one with POST /v1/projects/{project}/author-runs"
            )
        if state not in runs.TERMINAL_STATES:
            raise HTTPException(409, f"run {run_id} is still {state}: a run is rerun once it has ended")
        held = await plan_routes._visible(conn, access, plan_id, None)
        await _refuse_curator_plan(conn, access, plan_id)
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
        except TimeoutError:
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


async def _worker_of(conn: AsyncConnection, user: Principal):
    """The row of the worker whose token made the request, locked: id, owner_id, name, slots, runtimes, checkouts,
    drained_at, revoked_at, agent_version, dispatch_from and run_kinds. 403 when there is none."""
    w = tables.workers
    query = (
        select(
            w.c.id,
            w.c.owner_id,
            w.c.name,
            w.c.slots,
            w.c.runtimes,
            w.c.checkouts,
            w.c.drained_at,
            w.c.revoked_at,
            w.c.agent_version,
            w.c.dispatch_from,
            w.c.run_kinds,
        )
        .where(w.c.token_id == user.token_id)
        .with_for_update()
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None or row.revoked_at is not None:
        raise HTTPException(403, "this worker token belongs to no live worker: join the machine again")
    return row


def _served(worker_id: int, owner_id: int):
    """The projects worker ``worker_id`` serves on which its owner holds writer: their ids and names."""
    wp, p, g = tables.worker_projects, tables.projects, tables.grants
    writer = exists().where(g.c.user_id == owner_id, g.c.project_id == wp.c.project_id, g.c.role.in_(WRITER_ROLES))
    return (
        select(wp.c.project_id, p.c.name)
        .join_from(wp, p, p.c.id == wp.c.project_id)
        .where(wp.c.worker_id == worker_id, writer)
    )


def _claimable(
    worker_id: int,
    owner_id: int,
    projects: list[int],
    runtimes: list[str],
    pairs: list[tuple[int, str]],
    *,
    plan_runs: bool,
    web_only: bool,
    review_runs: bool = False,
    judge_runs: bool = False,
    author_runs: bool = False,
):
    """The oldest queued run the worker may take now, locked, passing over one another claim holds locked: of its
    owner, in ``projects``, pinned to no other worker, asking for any runtime or one of ``runtimes``, of a repo it
    has a checkout of (``pairs`` of project id and repo). A run of one step needs a checkout of its repo, and a plan
    run one of every repo in its repos and a daemon of runs.PLAN_RUN_AGENT or later (``plan_runs``); a review run one
    of every repo in its repos and a daemon that says it runs review runs (``review_runs``), and a judge run likewise
    (``judge_runs``); an author run one of its first repo, the project's harness, and a daemon that says it runs
    author runs (``author_runs``): the worker leaves out the other repos it has no checkout of. A worker set to take
    runs dispatched from the web only (``web_only``) passes over the others, those dispatched before schema 0011
    included."""
    r = tables.runs
    pids = literal([pid for pid, _ in pairs], ARRAY(BigInteger))
    repos = literal([repo for _, repo in pairs], ARRAY(Text))
    held = func.unnest(pids, repos).table_valued("project_id", "repo").render_derived(name="c")
    checkouts = select(held.c.project_id, held.c.repo).cte("checkouts")
    needed = func.jsonb_array_elements(r.c.repos).table_valued(column("entry", JSONB)).render_derived(name="needed")
    # two levels down, the run is correlated by name: auto-correlation reaches only the enclosing level
    has_needed = (
        exists()
        .where(checkouts.c.project_id == r.c.project_id, checkouts.c.repo == needed.c.entry["repo"].astext)
        .correlate(r, needed)
    )
    every_repo = ~exists().select_from(needed).where(~has_needed)
    own_repo = exists().where(checkouts.c.project_id == r.c.project_id, checkouts.c.repo == r.c.repo)
    harness = exists().where(checkouts.c.project_id == r.c.project_id, checkouts.c.repo == r.c.repos[0]["repo"].astext)
    query = select(r.c.id, r.c.runtime, r.c.kind).where(
        r.c.state == "queued",
        r.c.project_id.in_(projects),
        r.c.dispatched_by == owner_id,
        or_(r.c.pinned_worker_id.is_(None), r.c.pinned_worker_id == worker_id),
        or_(r.c.runtime == "any", r.c.runtime.in_(runtimes)),
        case((r.c.kind.in_(("plan", "review", "judge")), every_repo), (r.c.kind == "author", harness), else_=own_repo),
    )
    if web_only:
        query = query.where(r.c.dispatched_via == "web")
    if not plan_runs:
        query = query.where(r.c.kind != "plan")
    if not review_runs:
        query = query.where(r.c.kind != "review")
    if not judge_runs:
        query = query.where(r.c.kind != "judge")
    if not author_runs:
        query = query.where(r.c.kind != "author")
    return query.order_by(r.c.id).limit(1).with_for_update(of=r, skip_locked=True)


async def _current_plan(conn: AsyncConnection, project_id: int, plan_id: str):
    """(body, revision) of the plan as the hub holds it now; None when it is not on the hub."""
    plans = tables.plans
    query = select(plans.c.body, plans.c.revision).where(plans.c.project_id == project_id, plans.c.plan_id == plan_id)
    return (await conn.execute(query)).one_or_none()


class ClaimAbandoned(Exception):
    """The worker hung up before its claim was answered; the lease the claim took is rolled back."""

    def __init__(self, run_id: int):
        super().__init__(f"the worker hung up before run {run_id} was handed to it")
        self.run_id = run_id


def lease_of(request: Request) -> timedelta:
    """How long a claim and each heartbeat lease a run for: EVO_HUB_RUN_LEASE_SECONDS."""
    return timedelta(seconds=request.app.state.config.run_lease_seconds)


@dataclass(frozen=True)
class HeldSkill:
    """The latest version of a global skill, as the hub holds it."""

    name: str
    version: int
    sha256: str
    size: int


def _global_skill(name: str):
    """The latest version of the global skill ``name`` (ignoring case, as names are unique): name, version, sha256,
    size."""
    sk, v = tables.skills, tables.skill_versions
    return (
        select(sk.c.name, v.c.version, v.c.sha256, v.c.size)
        .join_from(sk, v, v.c.skill_id == sk.c.id)
        .where(sk.c.scope == "global", func.lower(sk.c.name) == name.lower())
        .order_by(v.c.version.desc())
        .limit(1)
    )


async def author_skill(conn: AsyncConnection) -> HeldSkill | None:
    """The version of ``author.AUTHOR_SKILL`` an author run claimed now gets; None when the hub has none."""
    row = (await conn.execute(_global_skill(author.AUTHOR_SKILL))).one_or_none()
    return None if row is None else HeldSkill(*row)


def _skill_refusal(skill: HeldSkill | None, blobs) -> str | None:
    """Why an author run cannot be handed its skill now, which fails it at its claim; None when it can."""
    name = author.AUTHOR_SKILL
    if skill is None:
        return (
            f"the hub holds no global skill {name}, which the agent of an author run writes the plan with: a hub admin "
            f"publishes it with `evo-agents hub skills publish <agent-skills>/skills/{name}` (scope global), then "
            "dispatch the author run again"
        )
    if blobs is None:
        return (
            f"the hub has no blob store, so it cannot hand the worker the bundle of skill {name} (version "
            f"{skill.version}): configure the blob store of the hub, then dispatch the author run again"
        )
    return None


async def _skill_ticket(blobs, skill: HeldSkill) -> RunSkill:
    """A presigned GET of ``skill``'s bundle, for the worker that claimed the run; it works MAX_GET_TTL."""
    expires_at = datetime.now(UTC) + MAX_GET_TTL  # taken before signing, so never later than the URL
    filename = f"{skill.name}-v{skill.version}.tar.gz"
    url = await asyncio.to_thread(blobs.presign_get, skill.sha256, MAX_GET_TTL, filename=filename)
    return RunSkill(
        name=skill.name, version=skill.version, sha256=skill.sha256, size=skill.size, url=url, expires_at=expires_at
    )


async def _try_claim(
    engine,
    user: Principal,
    lease: timedelta,
    gone: Callable[[], Awaitable[bool]] | None = None,
    blobs=None,
) -> RunSpec | None:
    """Lease the run the worker of ``user`` may take now, if any, for ``lease`` (see the module's docstring). When
    ``gone`` says the worker hung up once the run is leased, raise ClaimAbandoned before the transaction commits,
    which rolls the lease back. An author run comes with a presigned GET of its skill from the blob store ``blobs``;
    one the hub cannot hand its skill (none published, or no blob store) fails here, as the reaper, saying why, and
    the claim takes nothing this time."""
    async with engine.begin() as conn:
        found = await _worker_of(conn, user)
        worker_id, owner_id, name, slots, reported, checkouts, drained_at, _, version, dispatch_from, kinds = found
        if drained_at is not None:
            return None
        r = tables.runs
        holding = select(func.count()).select_from(r).where(r.c.worker_id == worker_id, r.c.state.in_(runs.HELD_STATES))
        if (await conn.execute(holding)).scalar_one() >= slots:
            return None
        runtimes = [runtime for runtime in runs.RUNTIMES if available((reported or {}).get(runtime))]
        if not runtimes:
            return None
        served = {row.project_id: row.name for row in await conn.execute(_served(worker_id, owner_id))}
        by_name = {project: project_id for project_id, project in served.items()}
        pairs = []
        for checkout in checkouts or {}:
            project, _, repo = checkout.partition("/")
            if project in by_name and repo:
                pairs.append((by_name[project], repo))
        if not pairs:
            return None
        query = _claimable(
            worker_id,
            owner_id,
            list(served),
            runtimes,
            pairs,
            plan_runs=runs.takes_plan_runs(version),
            web_only=dispatch_from == "web",
            review_runs="review" in (kinds or ()),
            judge_runs="judge" in (kinds or ()),
            author_runs="author" in (kinds or ()),
        )
        row = (await conn.execute(query)).one_or_none()
        if row is None:
            return None
        run_id, asked, kind = row
        skill = None
        if kind == "author":
            skill = await author_skill(conn)
            refused = _skill_refusal(skill, blobs)
            if refused is not None:
                await move_run(conn, run_id, "queued", "failed", "reaper", reason=refused, error=refused)
                log.warning("author run failed at its claim", extra={"run_id": run_id, "worker_id": worker_id})
                return None
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
        if skill is not None:
            spec.skills = [await _skill_ticket(blobs, skill)]
        if gone is not None and await gone():
            raise ClaimAbandoned(run_id)  # leaving the block rolls the transaction back
    log.info("run claimed", extra={"run_id": run_id, "worker_id": worker_id, "runtime": runtime})
    return spec


async def _run_spec(conn: AsyncConnection, run_id: int) -> RunSpec:
    view = await run_view(conn, run_id)
    r, revisions = tables.runs, tables.plan_revisions
    project_id = (await conn.execute(select(r.c.project_id).where(r.c.id == run_id))).scalar_one()
    dispatched = select(revisions.c.body).where(
        revisions.c.project_id == project_id,
        revisions.c.plan_id == view.plan_id,
        revisions.c.revision == view.plan_revision,
    )
    body = (await conn.execute(dispatched)).scalar_one_or_none()
    plan = body if body is not None else {"id": view.plan_id, "steps": []}
    copy = None
    if view.kind == "review":
        from evo_agents.hub.server.collect import review_prompt  # it queues runs through this module

        prompt = await review_prompt(conn, view)
        title = view.title
    elif view.kind == "author":
        skill = await author_skill(conn)
        repos = [repo.model_dump() for repo in view.repos or []]
        version = skill.version if skill is not None else 0
        prompt = author.build_author_prompt(view.project, view.dispatched_by, view.request or "", repos, version)
        title = view.title
    elif view.kind == "judge":
        from evo_agents.hub.server.changes import judge_prompt  # it queues runs through this module

        prompt = await judge_prompt(conn, view)
        title = view.title
    elif view.kind == "plan":
        current = await _current_plan(conn, project_id, view.plan_id)
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
        budget=await _claimed_budget(conn, view),
        curator=await _curator_spec(conn, project_id, view),
    )


async def _curator_spec(conn: AsyncConnection, project_id: int, view: Run) -> CuratorSpec | None:
    from evo_agents.hub.server.changes import run_curator  # it reads runs through this module

    found = await run_curator(conn, project_id, view)
    return None if found is None else CuratorSpec(**found)


async def _claimed_budget(conn: AsyncConnection, view: Run) -> ClaimedBudget | None:
    """The budget of run ``view`` with what it spent already: the agent time it counted (a run that resumes a parked
    one starts with that run's), and the cost of the run it resumes, whose session it goes on in."""
    if view.budget is None:
        return None
    spent_usd = 0.0
    if view.resume_of_run_id is not None:
        r = tables.runs
        usage = (await conn.execute(select(r.c.usage).where(r.c.id == view.resume_of_run_id))).scalar_one_or_none()
        spent_usd = curator.run_cost(usage)
    return ClaimedBudget(**view.budget.model_dump(), spent_usd=spent_usd, spent_seconds=view.run_seconds)


@worker_router.post("/claim", response_model=Claim, responses={403: {"model": ErrorBody}})
async def claim(request: Request, user: CurrentUser, body: ClaimRequest | None = None) -> Claim:
    """Wait up to ``wait_s`` seconds (25 by default) for a run this worker may take, and lease it."""
    wait = (body or ClaimRequest()).wait_s
    wakeups: RunWakeups = request.app.state.run_wakeups
    wakeups.start()
    engine = request.app.state.engine
    lease = lease_of(request)
    async with engine.begin() as conn:
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
                spec = await _try_claim(engine, user, lease, request.is_disconnected, request.app.state.blobs)
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


def _extend(worker_id: int, run_ids: list[int], lease: timedelta):
    """Extend the leases of the runs of ``run_ids`` the worker holds and settle their agent time; per run, its id,
    state, whether a cancel was asked, the new lease, and whether a takeover and a handback were asked."""
    r = tables.runs
    return (
        update(r)
        .values(lease_expires_at=func.now() + lease, **settle())
        .where(r.c.worker_id == worker_id, r.c.id.in_(run_ids), r.c.state.in_(runs.HELD_STATES))
        .returning(
            r.c.id,
            r.c.state,
            r.c.cancel_requested_at.is_not(None),
            r.c.lease_expires_at,
            r.c.takeover_requested_at.is_not(None),
            r.c.handback_requested_at.is_not(None),
        )
    )


def _parked_here(worker_id: int, run_ids: list[int]):
    """Runs of ``run_ids`` the worker should let go of without cancelling: parked, or done because a new run resumes
    them, which needs their session and worktrees on this worker."""
    r, resumed_by = tables.runs, tables.runs.alias("n")
    resumed = exists().where(resumed_by.c.resume_of_run_id == r.c.id)
    return select(r.c.id, r.c.state).where(
        r.c.worker_id == worker_id,
        r.c.id.in_(run_ids),
        or_(r.c.state == "parked", and_(r.c.state == "done", resumed)),
    )


def _per_run(run_id, *conditions):
    """How many rows match ``conditions`` for each run (``run_id``, a run_id column), runs without one left out."""
    return select(run_id, func.count().label("rows")).where(*conditions).group_by(run_id)


@worker_router.post("/heartbeat", response_model=HeartbeatAnswer, responses={403: {"model": ErrorBody}})
async def heartbeat(request: Request, body: HeartbeatRequest, user: CurrentUser) -> HeartbeatAnswer:
    """Record the machine, extend the leases of the runs it holds, and say what it should do with them."""
    reported = list(dict.fromkeys(body.runs))
    runtimes, checkouts = body.stored()
    async with request.app.state.engine.begin() as conn:
        worker_id, _, _, _, _, _, drained_at, *_ = await _worker_of(conn, user)
        w = tables.workers
        await conn.execute(
            update(w)
            .values(
                last_heartbeat_at=func.now(),
                runtimes=runtimes,
                checkouts=checkouts,
                free_slots=func.least(body.free_slots, w.c.slots),
                agent_version=func.coalesce(body.agent_version, w.c.agent_version),
                run_kinds=None if body.run_kinds is None else [k for k in body.run_kinds if k in runs.RUN_KINDS],
            )
            .where(w.c.id == worker_id)
        )
        extended = {row[0]: row[1:] for row in await conn.execute(_extend(worker_id, reported, lease_of(request)))}
        others = [run_id for run_id in reported if run_id not in extended]
        parked = {}
        if others:
            parked = {row.id: row.state for row in await conn.execute(_parked_here(worker_id, others))}
        known = [*extended, *parked]
        inbox, decisions = tables.run_inbox, tables.decisions
        waiting, open_decisions = {}, {}
        if extended:
            undelivered = _per_run(inbox.c.run_id, inbox.c.run_id.in_(list(extended)), inbox.c.delivered_at.is_(None))
            waiting = {row.run_id: row.rows for row in await conn.execute(undelivered)}
        if known:
            still_open = _per_run(decisions.c.run_id, decisions.c.run_id.in_(known), decisions.c.state == "open")
            open_decisions = {row.run_id: row.rows for row in await conn.execute(still_open)}
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


def _waits_for(run_id: int):
    """Whether run ``run_id`` has something to wait for: a decision still open, or an answer the worker has not taken
    for the agent yet."""
    decisions, inbox = tables.decisions, tables.run_inbox
    open_decision = exists().where(decisions.c.run_id == run_id, decisions.c.state == "open")
    answer = exists().where(inbox.c.run_id == run_id, inbox.c.decision_id.is_not(None), inbox.c.delivered_at.is_(None))
    return select(or_(open_decision, answer))


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
    async with request.app.state.engine.begin() as conn:
        worker_id, _, name, *_ = await _worker_of(conn, user)
        r = tables.runs
        reported_run = (
            select(r.c.state, r.c.approval, r.c.worker_id, r.c.cancel_requested_at, r.c.kind)
            .where(r.c.id == run_id)
            .with_for_update()
        )
        row = (await conn.execute(reported_run)).one_or_none()
        if row is None or row.worker_id != worker_id:
            raise HTTPException(404, NOT_HELD.format(id=run_id))
        state, approval, _, cancel_requested_at, kind = row
        columns = _reported_columns(body)
        if state == body.state:  # a resend, or news without a move: kept, and nothing moves
            news = {name: value for name, value in columns.items() if name in SAME_COLUMNS}
            if news:  # a column the report leaves out keeps its value
                await conn.execute(update(r).values(**news).where(r.c.id == run_id))
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
        if body.state == "waiting" and not (await conn.execute(_waits_for(run_id))).scalar_one():
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
        await credentials.revoke_tokens(
            app_state.engine, app_state.sealer, credentials.revoker(app_state), run_id=run_id
        )
    return view


def _check_verdict(run_id: int, kind: str, approval: str, body: StateReport) -> None:
    """409 for a verdict the run's approval, or its kind, does not allow. A plan run ends done without verify
    results, since each of its steps was verified when it was reported, and never waits in review."""
    if kind in ("plan", "review", "judge", "author"):
        if body.state == "review":
            article = "an" if kind[:1] in "aeiou" else "a"
            raise HTTPException(409, f"run {run_id} is {article} {kind} run: report done or failed, not review")
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


async def _held_plan_run(conn: AsyncConnection, user: Principal, run_id: int, *, lock: bool = False):
    """(worker name, row) of a plan run the worker of ``user`` holds, its row locked with ``lock``: state, worker_id,
    kind, project_id, the project's name, plan_id, dispatched_by, the dispatcher's login and repos. 404 for any other
    run, a run of one step included."""
    worker_id, _, name, *_ = await _worker_of(conn, user)
    r, p, u = tables.runs, tables.projects, tables.users
    query = (
        select(
            r.c.state,
            r.c.worker_id,
            r.c.kind,
            r.c.project_id,
            p.c.name,
            r.c.plan_id,
            r.c.dispatched_by,
            u.c.login,
            r.c.repos,
        )
        .join_from(r, p, p.c.id == r.c.project_id)
        .join(u, u.c.id == r.c.dispatched_by)
        .where(r.c.id == run_id)
    )
    if lock:
        query = query.with_for_update(of=r)
    row = (await conn.execute(query)).one_or_none()
    if row is None or row.worker_id != worker_id or row.state not in runs.HELD_STATES:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    if row.kind != "plan":
        what = {"review": "a review run", "judge": "a judge run", "author": "an author run"}.get(
            row.kind, "a run of one step"
        )
        raise HTTPException(
            404,
            f"run {run_id} is {what}, which has no plan to read or steps to report: report its state with "
            f"POST /v1/worker/runs/{run_id}/state",
        )
    return name, row


async def _dispatcher_access(conn: AsyncConnection, row, user: Principal) -> ProjectAccess:
    """The access to the run's project of the member who dispatched it, as whom the worker reads and writes the plan."""
    actor = Principal(row[6], row[7], False, user.token_id, MACHINE, "")
    access = await project_access(conn, actor, row[4])
    plan_routes._reader(access)
    return access


@worker_router.get("/runs/{run_id}/plan", response_model=plan_routes.Plan, responses=REFUSALS)
async def read_run_plan(request: Request, run_id: RunId, user: CurrentUser):
    """The plan of a plan run this worker holds, as the hub holds it now, read as the member who dispatched the run."""
    async with request.app.state.engine.begin() as conn:
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
    async with request.app.state.engine.begin() as conn:
        _, row = await _held_plan_run(conn, user, run_id, lock=True)
        _, _, _, project_id, project, plan_id, dispatcher_id, _, repos = row
        _check_step_report(run_id, key, body)
        current = await _current_plan(conn, project_id, plan_id)
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
        after = await _current_plan(conn, project_id, plan_id)
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
