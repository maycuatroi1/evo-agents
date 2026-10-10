"""The run API's shapes and limits: request and response models, the path and query types of its routes, and the
refusals and messages they share. The lowest layer of ``evo_agents.hub.server.runs``: it imports neither the routes
nor the service (lint-imports, contract hub-runs-layers), and the modules around the run API import their models and
limits from here."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import Path
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from evo_agents.hub import author, runs
from evo_agents.hub.credentials import DISPATCHED_VIA
from evo_agents.hub.plans import PLAN_ID
from evo_agents.hub.runs import MAX_EVIDENCE_BYTES
from evo_agents.hub.server.errors import ErrorBody

MAX_ID = 2**63 - 1  # bigint
MAX_DISPATCH_STEPS = 50
MAX_HEARTBEAT_RUNS = 64
MAX_REPORT_BYTES = 64 * 1024  # runtimes and checkouts of a heartbeat, or the usage of a state report, as JSON
LINE = r"^[^\x00-\x1f\x7f]+$"
OBJECT_NAME = r"^([0-9a-f]{40}|[0-9a-f]{64})$"
CHECKOUT_KEY = r"^[a-z0-9][a-z0-9-]{0,99}/[^\x00-\x1f\x7f/][^\x00-\x1f\x7f]{0,199}$"  # <project>/<repo>
STEP_KEY_CHARS = 200
REQUESTED_RUNTIMES = ("any", *runs.RUNTIMES)
NOT_HELD = "this worker does not hold run {id}: it may have been lost, cancelled or taken by another attempt"
STEP_REPORT_STATUSES = ("in_progress", "done", "pending")  # what a plan run's worker reports of a step
RUN_KIND = r"^[a-z][a-z0-9_]{0,31}$"  # a kind of run a heartbeat names, known to this hub or not
MAX_RUN_KINDS = 20
MAX_STEP_EVIDENCE_CHARS = MAX_EVIDENCE_BYTES  # the agent's evidence of one step; the hub's adds to it, within the bytes

RunId = Annotated[int, Path(ge=1, le=MAX_ID)]
RuntimeName = Literal[runs.RUNTIMES]  # an alias: a model with a field named runs cannot say runs.RUNTIMES
ModelName = Annotated[str, Field(min_length=1, max_length=runs.MAX_MODEL_CHARS, pattern=LINE)]
REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 409: {"model": ErrorBody}}
StepKey = Annotated[str, Path(min_length=1, max_length=STEP_KEY_CHARS, description="the step's id, or its order")]

MAX_LIST = 200
MAX_OFFSET = 100_000
STATS_DAYS = 30  # the days GET .../runs/stats covers without ?days, today in UTC the last of them
MIN_STATS_DAYS, MAX_STATS_DAYS = 7, 90


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
    plan_id: str = Field(pattern=PLAN_ID)
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
    plan_id: str | None = Field(
        None,
        pattern=PLAN_ID,
        description="the plan the run revises, one you can read; null: the run writes a new plan",
    )
    runtime: Literal[REQUESTED_RUNTIMES] = Field(
        "claude-code",
        description="claude-code, the one runtime an author run takes (any means it); another one is 422",
    )
    model: ModelName | None = Field(None, description="the model to use, as Claude Code names it; null: its own choice")
    timeout_h: Literal[author.AUTHOR_TIMEOUT_CHOICES] = Field(
        author.DEFAULT_TIMEOUT_H, description="hours of agent time the run may take"
    )


class PlanRunDispatch(BaseModel):
    plan_id: str = Field(pattern=PLAN_ID)
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


class SteadyWait(BaseModel):
    """The worker a queued attempt waits for: the run before it was lost on that worker, which may take this one only
    once it is steady, its heartbeats come for 120 seconds with none more than 30 seconds late (runs.STEADY_SECONDS,
    runs.STEADY_GAP_SECONDS)."""

    worker_id: int
    worker: str = Field(description="that worker's name")
    steady_at: datetime | None = Field(
        description="when the worker becomes steady if its heartbeats go on as now; null while it sends none"
    )


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
    finish_requested_at: datetime | None = Field(
        None, description="when the owner ended the chat of an author run its worker holds; null otherwise"
    )
    steady_wait: SteadyWait | None = Field(
        None,
        description="a queued attempt after a run lost on a worker that is not steady yet: that worker takes it only "
        "once it is; another worker may take it now unless it is pinned to that one. Null otherwise",
    )
    failure_cause: str | None = Field(
        None,
        description="why the run failed, as its worker reported it: origin, credentials or missing_tool when its "
        "preflight stopped it before the agent started, else push_conflict, verify_failed, timeout and the others of "
        "runs.FAILURE_CAUSES; null for a run that did not fail, one an older daemon or the hub ended, and every run "
        "from before schema 0024",
    )


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
    verify: list[str] | None = Field(
        None,
        description="the verify of the step of a run of one step, as the plan it was dispatched from has it: the "
        "worker's preflight checks that each program it calls is on PATH before the agent starts; null for any other "
        "kind, whose worker reads the verify elsewhere (a plan run's plan, a judge run's inputs)",
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
    finish: bool = Field(
        False,
        description="the owner ended the chat of this author run: once the agent's turn is over, end the run done",
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
    failure_cause: str | None = Field(
        None,
        pattern=runs.FAILURE_CAUSE,
        description="with failed: why, one of runs.FAILURE_CAUSES (a newer worker's own names are kept too); kept with "
        "the run as its failure_cause, and ignored with any other state",
    )
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


# Listing runs and their stats

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


# A plan run's plan


class AuthoredPlan(BaseModel):
    body: dict[str, Any] = Field(description="the plan as its YAML file reads; a hub key in it is ignored")
    if_revision: int | None = Field(
        None, ge=0, description="the revision being replaced; leave it out, or 0, to create the plan"
    )
    label: dict | None = Field(None, description="{level, location, integrity}; default: the project's, or the held")
