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

from evo_agents.hub import author, profiles, runs
from evo_agents.hub.credentials import DISPATCHED_VIA, MAX_SECRET_NAME_CHARS, SECRET_NAME
from evo_agents.hub.plans import PLAN_ID
from evo_agents.hub.runs import MAX_EVIDENCE_BYTES
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.plugins.config import MAX_LAYER_BYTES
from evo_agents.plugins.manifest import PLUGIN_ID

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
MAX_SELF_UPDATE_REASON = 500  # characters of self_update.reason in a heartbeat
MAX_SELF_UPDATE_ERROR = 2000  # characters of self_update.error in a heartbeat
MAX_STEP_EVIDENCE_CHARS = MAX_EVIDENCE_BYTES  # the agent's evidence of one step; the hub's adds to it, within the bytes

RunId = Annotated[int, Path(ge=1, le=MAX_ID)]
# Any runtime's name, any included: one of the core's or a plugin's <plugin>/<name>. A dispatch takes one registered in
# its project (``service.plugin_runtimes``), else 422.
RuntimeName = Annotated[str, Field(pattern=runs.RUNTIME_NAME, max_length=127)]
RUNTIME_HELP = (
    "any: the claiming worker picks a runtime of the core it has; else claude-code, opencode, codex, or "
    "<plugin>/<name>, the runtime a plugin enabled on the project declares; a runtime the project does not register is "
    "422"
)
ModelName = Annotated[str, Field(min_length=1, max_length=runs.MAX_MODEL_CHARS, pattern=LINE)]
REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 409: {"model": ErrorBody}}
StepKey = Annotated[str, Path(min_length=1, max_length=STEP_KEY_CHARS, description="the step's id, or its order")]
AccountName = Annotated[str, Field(max_length=MAX_SECRET_NAME_CHARS, pattern=f"^{SECRET_NAME.pattern}$")]
ProfileHandle = Annotated[str, Field(pattern=profiles.HANDLE_PATTERN)]
PROFILE_HELP = (
    "the agent profile the run goes under, by handle (GET /v1/projects/{p}/agents): what its runs may use; the hub "
    "refuses a dispatch its allowlists leave out (422). Null: the project's default profile, which allows everything"
)
PluginKey = Annotated[str, Field(pattern=PLUGIN_ID)]
PLUGIN_CONFIG_HELP = (
    "the run's own layer of the config of plugins enabled on the project, above the agent profile's: plugin name to "
    "the plain values it sets, checked against the plugin's schema; no secret (422). Null: none"
)
ACCOUNT_HELP = (
    "one of the caller's env secrets bound to the project, by name (GET /v1/accounts): the run takes it for its "
    "variable and keeps it, with no failover to another account; null: the account is picked by the rules of the "
    "caller's policy for the project"
)

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
    runtime: RuntimeName = Field("any", description=RUNTIME_HELP)
    mode: Literal[runs.MODES] = "headless"
    worker_id: int | None = Field(None, ge=1, le=MAX_ID, description="pin the runs to this worker of yours")
    approval: Literal[runs.APPROVALS] = Field("review", description="auto: verified runs mark the step done")
    timeout_min: int = Field(60, ge=5, le=240)
    model: ModelName | None = Field(
        None,
        description="the model each run uses, as its runtime names it (opencode: provider/model); null: the "
        "runtime's own choice",
    )
    account: AccountName | None = Field(None, description=ACCOUNT_HELP)
    profile: ProfileHandle | None = Field(None, description=PROFILE_HELP)
    plugin_config: dict[PluginKey, dict[str, Any]] | None = Field(None, max_length=50, description=PLUGIN_CONFIG_HELP)

    @model_validator(mode="after")
    def _bounded_plugin_config(self) -> Dispatch:
        if self.plugin_config and _json_bytes(self.plugin_config) > MAX_LAYER_BYTES:
            raise ValueError(f"plugin_config is at most {MAX_LAYER_BYTES // 1024} KiB as JSON")
        return self


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
    account: AccountName | None = Field(None, description=ACCOUNT_HELP)
    profile: ProfileHandle | None = Field(None, description=PROFILE_HELP)


class PlanRunDispatch(BaseModel):
    plan_id: str = Field(pattern=PLAN_ID)
    worker_id: int | None = Field(None, ge=1, le=MAX_ID, description="pin the run to this worker of yours")
    runtime: RuntimeName = Field("any", description=RUNTIME_HELP)
    model: ModelName | None = Field(
        None, description="the model to use, as its runtime names it (opencode: provider/model); null: the runtime's"
    )
    mode: Literal[runs.MODES] = "headless"
    timeout_h: Literal[runs.PLAN_TIMEOUT_CHOICES] = Field(
        4, description="hours of agent time the run may take; waiting for a decision and parked do not count"
    )
    session_per_step: bool = Field(
        False,
        description="true: the worker starts a new agent session for each step, whose prompt is the step's with the "
        "handoff of the plan, and only a worker whose heartbeat lists the feature session_per_step takes the run; "
        "false: one session does every step",
    )
    account: AccountName | None = Field(None, description=ACCOUNT_HELP)
    profile: ProfileHandle | None = Field(None, description=PROFILE_HELP)


class Rerun(BaseModel):
    """What a rerun may change of the run it reruns; no body keeps everything."""

    model_config = ConfigDict(extra="forbid")

    account: AccountName | None = Field(
        None,
        description="one of the caller's env secrets bound to the project, by name: the rerun takes it, with no "
        "failover; null keeps the account picked by hand for the run rerun, if one was, else the rules pick",
    )
    account_by_rule: bool = Field(
        False, description="true: the rules of the caller's policy pick the account, not the one picked for the run"
    )

    @model_validator(mode="after")
    def _one_choice(self) -> Rerun:
        if self.account is not None and self.account_by_rule:
            raise ValueError("name an account or ask for one by rule, not both")
        return self


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
    kind: Literal[runs.SHOWN_KINDS] = Field(
        description="step: one step of the plan; plan: every step not done yet; review: the night's review of the "
        "project by the Curator, on no plan; judge: the Curator's Judge of a change of its plan; author: a plan "
        "written from a member's request, on no plan when it writes a new one; chat: one turn of an agent mentioned in "
        "a group chat, on no plan; local: a Claude Code session on a member's own machine, which no worker runs and "
        "the hub only watches"
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
    requested_runtime: str = Field(description="as the dispatch asked: any, a runtime of the core or a plugin's")
    runtime: str = Field(description="any until a worker claims the run; a plugin's runtime is <plugin>/<name>")
    model: str | None = Field(description="the model the dispatch asked for; null: the runtime's own choice")
    mode: Literal[runs.MODES]
    approval: Literal[runs.APPROVALS]
    timeout_min: int
    run_seconds: int = Field(description="the agent time the run has used of its timeout, as the hub last counted it")
    attempt: int
    max_attempts: int
    parent_run_id: int | None = Field(description="the run this one retries or reruns")
    resume_of_run_id: int | None = Field(description="the parked plan run this one goes on from, in its session")
    state: Literal[runs.SHOWN_STATES] = Field(
        description="idle and ended are a local run's alone: the turn ended and the person has the word; the session "
        "is over, or went silent for a day"
    )
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
    session_per_step: bool = Field(
        False,
        description="a plan run whose worker starts a new agent session for each step; its usage lists the sessions "
        "(usage.sessions). False for any other run",
    )
    profile: str | None = Field(
        None, description="the handle of the agent profile the run went under; null for a run from before schema 0030"
    )
    profile_id: int | None = Field(None, description="that profile's id, which its handle names for good")
    profile_version: int | None = Field(None, description="the version of the profile whose rules apply to the run")
    environment: str | None = Field(
        None,
        description="the environment of a plugin, <plugin>/<name>, its claim picked from the environment axis of its "
        "profile, a container its agent runs in; null for a run on the worker's host, and before a claim",
    )
    chat_id: int | None = Field(None, description="a chat run's group chat; null for any other run")
    chat_message_id: int | None = Field(
        None, description="the message of the chat that started a chat run: the mention, or the reply that woke it"
    )
    hop: int | None = Field(
        None,
        description="a chat run's hop: 1 for a person's mention, one more for each agent's mention after it (at most "
        "3); null for any other run",
    )
    machine: str | None = Field(None, description="a local run's machine, as its hub login names it; null otherwise")
    cwd: str | None = Field(None, description="the directory a local run's session works in; null otherwise")
    claude_email: str | None = Field(
        None, description="the Claude account that ran a local run's session, or unknown; null for any other run"
    )
    claude_profile: str | None = Field(None, description="the claude-profiles profile of a local run, when it was read")
    last_activity_at: datetime | None = Field(
        None, description="the last time a local run's session said anything; null for any other run"
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


class RunProfile(BaseModel):
    """The agent profile of a claimed run, at the version the run went under, for its worker to apply what the hub
    cannot (``evo_agents.hub.profiles``): tool, skill and model in the agent session, and the instructions."""

    handle: str
    version: int
    name: str
    instructions: str = Field(description="to add to the agent's system prompt; empty for none")
    permissions: dict = Field(
        description="the eight axes, each {mode: all} or {mode: allowlist, allow: [...]}; an axis a version from "
        "before the axis does not name allows all"
    )


class RunPack(BaseModel):
    """An agent pack the run's profile attaches (``evo_agents.plugins.agent_packs``), as the hub holds the version of
    its plugin the profile pins: the worker downloads the plugin's bundle, checks it, and builds the pack from its
    ``packs/<name>/`` into the run's directory for the agent's runtime."""

    pack: str = Field(description="<plugin>/<name>")
    plugin: str
    name: str
    version: str = Field(description="the version of the plugin the profile pins")
    sha256: str = Field(description="what the downloaded bundle must hash to")
    size: int
    url: str = Field(description="presigned GET of the plugin's bundle; a bearer credential until it expires")
    expires_at: datetime


class PluginRuntime(BaseModel):
    """A runtime a plugin enabled on a project declares (``evo_agents.plugins.runtimes``): the command of an ACP agent,
    at the plugin's version the hub has installed."""

    runtime: str = Field(description="<plugin>/<name>, as runs name it")
    plugin: str
    version: str = Field(description="the plugin's installed version")
    title: str
    command: list[str] = Field(
        description="the program, found on the worker's PATH unless absolute, then its arguments"
    )
    models: list[str] = Field(description="the models the agent offers, which the worker reports")
    secrets: list[str] = Field(
        description="the variables the agent needs, from the run's leases or the worker's environment"
    )


class RunMount(BaseModel):
    """A mount of a plugin's environment (``evo_agents.plugins.run_environments.Mount``)."""

    type: Literal["tmpfs", "volume"]
    target: str = Field(description="the absolute path in the container")
    name: str | None = Field(
        None, description="a volume's name within the plugin; the worker names it evo-<plugin>-<name>"
    )
    read_only: bool = False
    size_mb: int | None = Field(None, description="a tmpfs's size; null: the engine's default")


class RunEnvironment(BaseModel):
    """An environment a plugin enabled on a project declares (``evo_agents.plugins.run_environments``): a container
    image pinned by its digest, at the plugin's version the hub has installed."""

    environment: str = Field(description="<plugin>/<name>, as the environment axis of a profile names it")
    plugin: str
    version: str = Field(description="the plugin's installed version")
    title: str
    provider: Literal["container"] = "container"
    image: str = Field(description="[registry/]repository[:tag]@sha256:<digest>, pulled and run by that digest")
    network: Literal["none", "outbound"]
    mounts: list[RunMount] = Field(default_factory=list, description="besides the worktree and the run's home")


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
    runtime: str = Field(description="a runtime of the core, or <plugin>/<name>, run with plugin_runtime's declaration")
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
    session_per_step: bool = Field(
        False,
        description="a plan run to run with a new agent session for each step not done yet, each with its prompt "
        "and the handoff of the plan; false: one session does every step, with the prompt above",
    )
    profile: RunProfile | None = Field(
        None, description="the agent profile the run went under; null for a run from before schema 0030"
    )
    plugin_runtime: PluginRuntime | None = Field(
        None,
        description="for a run of a plugin's runtime, its declaration at the plugin's version installed now, which the "
        "worker runs through its adapter of ACP; null for a runtime of the core",
    )
    environment: RunEnvironment | None = Field(
        None,
        description="the environment of a plugin the claim picked from the run's profile, at the plugin's version "
        "installed now, which the worker's container provider runs the agent in; null: the worker's host",
    )
    packs: list[RunPack] = Field(
        default_factory=list,
        description="the agent packs the run's profile attaches, in its order, each at the version it pins, which the "
        "worker builds into the run's directory for its runtime; empty for a profile that attaches none",
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


class SelfUpdateReport(BaseModel):
    """What the daemon last found of updating itself to the tag nightly of evo-agents."""

    state: str = Field(
        pattern=RUN_KIND,
        description="current, it runs the commit nightly points to; waiting, nightly points to another one and it "
        "waits until every daemon of its install is idle; updating, it is installing that commit and restarts on it; "
        "locked, its owner locked the worker's version; unsupported, it cannot update itself (reason says why). A "
        "state this hub does not know is kept as named (runs.SELF_UPDATE_STATES)",
    )
    reason: str | None = Field(
        None, min_length=1, max_length=MAX_SELF_UPDATE_REASON, description="why, with unsupported above all"
    )
    nightly_commit: str | None = Field(
        None, pattern=OBJECT_NAME, description="the commit nightly pointed to when the daemon last read it"
    )
    checked_at: datetime | None = Field(None, description="when the daemon last read where nightly points")
    error: str | None = Field(
        None,
        min_length=1,
        max_length=MAX_SELF_UPDATE_ERROR,
        description="the last attempt that failed: reading the tag, installing, or the new install not starting",
    )
    error_at: datetime | None = Field(None, description="when that attempt failed")


class HeartbeatRequest(BaseModel):
    runtimes: dict[RuntimeName, RuntimeReport] = Field(
        default_factory=dict,
        description='the runtimes on the machine: {"claude-code": {"available": true, "version": "2.1.289"}}, and '
        "those of the plugins its owner accepted that the hub sent it (plugin_runtimes of the answer), as "
        "<plugin>/<name>",
    )
    checkouts: dict[Annotated[str, Field(pattern=CHECKOUT_KEY)], CheckoutReport] = Field(
        default_factory=dict,
        max_length=500,
        description='keyed <project>/<repo>: {"evo-agents/evo-agents": {"path": "/src/evo-agents", "branch": "main"}}',
    )
    free_slots: int = Field(ge=0, le=runs.MAX_SLOTS)
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
    features: list[Annotated[str, Field(pattern=RUN_KIND)]] | None = Field(
        None,
        max_length=MAX_RUN_KINDS,
        description="what the daemon does besides its kinds of run: session_per_step, a plan run with a session for "
        "each step; author_decisions, an author run whose agent asks decisions and waits for the answers of a turn; "
        "agent_profiles, a run whose agent profile limits tool, skill or model, or has instructions, applied in the "
        "agent session; plugin_runtimes, a run of a plugin's runtime through its adapter of ACP; environments, a run "
        "whose agent runs in a container of a plugin's environment. One that says none "
        "takes no such run, and its author run's agent asks in the chat alone. A feature this hub does not know is "
        "left out",
    )
    agent_commit: str | None = Field(
        None,
        pattern=OBJECT_NAME,
        description="the commit of evo-agents the daemon's install was built from; none for an install that names "
        "no commit (PyPI, a local checkout) and from an older daemon. Each heartbeat sets what the hub shows",
    )
    self_update: SelfUpdateReport | None = Field(
        None,
        description="what the daemon last found of updating itself to the tag nightly; each heartbeat sets what the "
        "hub shows, so a daemon sends it every time once it has one",
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


class WorkerSettings(BaseModel):
    slots: int = Field(ge=1, le=runs.MAX_SLOTS, description="runs the worker holds at once")
    name: str
    labels: list[str]
    plugins: list[str] = Field(
        default_factory=list,
        description="the plugins whose runtimes and environments the worker takes; a hub before 0039 sends none",
    )
    version_locked: bool = Field(
        description="its owner locked its version: the daemon stays on the evo-agents it runs and does not update "
        "itself; a hub before 0048 sends none, and a daemon that got none does not update itself either"
    )


class HeartbeatAnswer(BaseModel):
    drain: bool = Field(description="claim no new run; finish the ones held")
    runs: list[RunControl]
    settings: WorkerSettings = Field(
        description="the worker's settings as the hub holds them, which its owner may change (PATCH "
        "/v1/workers/{id}): a daemon whose config says otherwise takes these; a hub before schema 0026 sends none"
    )
    plugin_runtimes: list[PluginRuntime] = Field(
        default_factory=list,
        description="for a daemon that says plugin_runtimes: the runtimes of the plugins of settings.plugins that a "
        "project the worker serves enabled, which it reports in runtimes once it found their programs; empty otherwise",
    )


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
    **{state: (int, Field(0, ge=0)) for state in runs.SHOWN_STATES},
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
