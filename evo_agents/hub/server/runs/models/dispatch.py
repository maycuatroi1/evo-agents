"""Dispatching runs and reading them: the requests that queue runs, the readiness of a plan's steps, and a run as
the API shows it."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evo_agents.hub import author, runs
from evo_agents.hub.credentials import DispatchedVia
from evo_agents.hub.plans import PLAN_ID
from evo_agents.hub.server.runs.models import browser as browser_help
from evo_agents.hub.server.runs.models.limits import (
    ACCOUNT_HELP,
    AS_ME_HELP,
    MAX_DISPATCH_STEPS,
    PLUGIN_CONFIG_HELP,
    PROFILE_HELP,
    RUNTIME_HELP,
    STEP_KEY_CHARS,
    AccountName,
    ModelName,
    PluginKey,
    ProfileHandle,
    RequestedRuntime,
    RuntimeName,
    json_bytes,
)
from evo_agents.hub.server.runs.models.placement import HubPlaced, RunPlacement
from evo_agents.hub.server.runs.models.routing import RoutingRecord
from evo_agents.hub.server.runs.models.run_parts import RunAutonomy, RunBudget, RunRepo
from evo_agents.hub.server.runs.models.spend import RunSpend
from evo_agents.plugins.config import MAX_LAYER_BYTES


class ActiveRun(BaseModel):
    id: int
    state: runs.ActiveState
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


class Dispatch(HubPlaced):
    plan_id: str = Field(pattern=PLAN_ID)
    steps: list[int | Annotated[str, Field(min_length=1, max_length=STEP_KEY_CHARS)]] = Field(
        min_length=1, max_length=MAX_DISPATCH_STEPS, description="step ids (or orders), one run each"
    )
    runtime: RuntimeName = Field("any", description=RUNTIME_HELP)
    mode: runs.Mode = "headless"
    approval: runs.Approval = Field("review", description="auto: verified runs mark the step done")
    timeout_min: int = Field(60, ge=5, le=240)
    model: ModelName | None = Field(
        None,
        description="the model each run uses, as its runtime names it (opencode: provider/model); null: the "
        "runtime's own choice",
    )
    account: AccountName | None = Field(None, description=ACCOUNT_HELP)
    profile: ProfileHandle | None = Field(None, description=PROFILE_HELP)
    plugin_config: dict[PluginKey, dict[str, Any]] | None = Field(None, max_length=50, description=PLUGIN_CONFIG_HELP)
    browser: bool = Field(False, description=browser_help.DISPATCH_HELP)
    identities: list[browser_help.IdentityName] = browser_help.identities_field()
    as_me: bool = Field(False, description=AS_ME_HELP)

    @model_validator(mode="after")
    def _bounded_plugin_config(self) -> Dispatch:
        if self.plugin_config and json_bytes(self.plugin_config) > MAX_LAYER_BYTES:
            raise ValueError(f"plugin_config is at most {MAX_LAYER_BYTES // 1024} KiB as JSON")
        return self


class AuthorRunDispatch(HubPlaced):
    request: str = Field(
        min_length=1,
        max_length=author.MAX_REQUEST_BYTES,
        description=f"what the plan should achieve, in the member's words: at most {author.MAX_REQUEST_BYTES} bytes "
        "of UTF-8",
    )
    plan_id: str | None = Field(
        None,
        pattern=PLAN_ID,
        description="the plan the run revises, one you can read; null: the run writes a new plan",
    )
    runtime: RequestedRuntime = Field(
        "claude-code",
        description="claude-code, the one runtime an author run takes (any means it); another one is 422",
    )
    model: ModelName | None = Field(None, description="the model to use, as Claude Code names it; null: its own choice")
    timeout_h: author.AuthorTimeout = Field(
        author.DEFAULT_TIMEOUT_H, description="hours of agent time the run may take"
    )
    account: AccountName | None = Field(None, description=ACCOUNT_HELP)
    profile: ProfileHandle | None = Field(None, description=PROFILE_HELP)


class PlanRunDispatch(HubPlaced):
    plan_id: str = Field(pattern=PLAN_ID)
    runtime: RuntimeName = Field("any", description=RUNTIME_HELP)
    model: ModelName | None = Field(
        None, description="the model to use, as its runtime names it (opencode: provider/model); null: the runtime's"
    )
    mode: runs.Mode = "headless"
    timeout_h: runs.PlanTimeout = Field(
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
    browser: bool = Field(False, description=browser_help.DISPATCH_HELP)
    identities: list[browser_help.IdentityName] = browser_help.identities_field()
    as_me: bool = Field(False, description=AS_ME_HELP)


class Rerun(HubPlaced):
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
    browser: bool | None = Field(None, description=browser_help.RERUN_HELP)
    as_me: bool | None = Field(None, description=AS_ME_HELP + "; null keeps the choice of the run rerun")

    @model_validator(mode="after")
    def _one_choice(self) -> Rerun:
        if self.account is not None and self.account_by_rule:
            raise ValueError("name an account or ask for one by rule, not both")
        return self


class AccessChange(BaseModel):
    """What PUT /v1/projects/{p}/runs/{id}/access changes of a run of the caller's."""

    model_config = ConfigDict(extra="forbid")

    as_me: bool = Field(
        description=AS_ME_HELP + "; false: it stops, and its token ends at once, as a run that never ran as you"
    )


class OverlapWait(BaseModel):
    """The runs a queued step run waits for: they work on the same plan and repo and have edited files the step names
    in its what or note, so the hub leaves the run in the queue until they end."""

    run_ids: list[int] = Field(description="the running step runs it waits for, the oldest first")
    paths: list[str] = Field(description="the files they edited that the step names, at most 10")


class SteadyWait(BaseModel):
    """The worker a queued attempt waits for: the run before it was lost on that worker, which may take this one only
    once it is steady, its heartbeats come for 120 seconds with none more than 30 seconds late (runs.STEADY_SECONDS,
    runs.STEADY_GAP_SECONDS)."""

    worker_id: int
    worker: str = Field(description="that worker's name")
    steady_at: datetime | None = Field(
        description="when the worker becomes steady if its heartbeats go on as now; null while it sends none"
    )


class PlanProgress(BaseModel):
    """How far a plan run's plan has come, as the plan holds it now."""

    done: int = Field(description="steps with status done")
    total: int = Field(description="steps of the plan")
    percent: int = Field(description="done of total, rounded down; 0 for a plan without steps")
    current_step: str | None = Field(
        description="the step in progress, else the first not done: its title, else its key; null once all are done"
    )
    current_status: str | None = Field(description="that step's status as the plan holds it")


class Run(RunPlacement):
    id: int
    kind: runs.ShownKind = Field(
        description="step: one step of the plan; plan: every step not done yet; review: the night's review of the "
        "project by the Curator, on no plan; judge: the Curator's Judge of a change of its plan, or a Judge of a merge "
        "request of the merge queue, on no plan; author: a plan written from a member's request, on no plan when it "
        "writes a new one; chat: one turn of an agent mentioned in a group chat, on no plan; release: a cut or a fix of "
        "the project's release cycle, on no plan; local: a Claude Code session on a member's own machine, which no "
        "worker runs and the hub only watches"
    )
    project: str
    plan_id: str = Field(
        description="the plan it works on; empty for a run on none: a review run, an author run of a new plan, a "
        "judge run of the merge queue"
    )
    step_key: str | None = Field(description="null for a plan run")
    title: str | None = Field(description="the step's title when the run was dispatched; the plan's for a plan run")
    plan_revision: int | None = Field(
        description="the plan revision the run was dispatched from; null for a run on no plan"
    )
    dispatched_by: str = Field(description="the login of the member who dispatched it, its owner")
    dispatched_via: DispatchedVia | None = Field(
        description="the credential it was dispatched with: web, a web session; machine, a token (the command line, "
        "an agent); schedule, the night shift of the project's charter, for its owner; release, the project's "
        "release schedule, for its owner; null for a run dispatched before 0.5.0"
    )
    worker_id: int | None = Field(description="the worker that claimed it")
    worker: str | None = Field(description="that worker's name")
    pinned_worker_id: int | None
    requested_runtime: str = Field(description="as the dispatch asked: any, a runtime of the core or a plugin's")
    runtime: str = Field(description="any until a worker claims the run; a plugin's runtime is <plugin>/<name>")
    model: str | None = Field(description="the model the dispatch asked for; null: the runtime's own choice")
    model_used: str | None = Field(
        None, description="the model its runtime ran it on, as its worker reported it; null until then, or not said"
    )
    routing: RoutingRecord | None = Field(
        None, description="what the routing by difficulty did; null for a run it did not rate"
    )
    mode: runs.Mode
    approval: runs.Approval
    timeout_min: int
    run_seconds: int = Field(description="the agent time the run has used of its timeout, as the hub last counted it")
    attempt: int
    max_attempts: int
    parent_run_id: int | None = Field(description="the run this one retries or reruns")
    resume_of_run_id: int | None = Field(description="the parked plan run this one goes on from, in its session")
    spawned_by_run_id: int | None = Field(
        None,
        description="the chat run whose agent asked the agent of this one, of another project (worker agent ask); "
        "null for a run no agent asked that way",
    )
    state: runs.ShownState = Field(
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
    spend: RunSpend | None = Field(
        None,
        description="what the run spent itself, by model, from its usage reports as they came: while it runs, waits "
        "or is parked too, and only its own share of a session it went on in; null before its first report",
    )
    plan_progress: PlanProgress | None = Field(
        None, description="a plan run's plan progress, set in a list of runs; null for any other run"
    )
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
    overlap_wait: OverlapWait | None = Field(
        None,
        description="a queued step run that waits for another run of the same plan and repo, which has edited files "
        "the step names; null otherwise",
    )
    warning: str | None = Field(
        None,
        description="set on the answer of a dispatch alone: no worker could take the run as it queued, why, and the "
        "command that fixes it (`evo-agents hub run candidates` says more); null otherwise",
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
    browser: bool = Field(False, description=browser_help.RUN_HELP)
    browser_identities: list[int] = Field([], description=browser_help.RUN_IDENTITIES_HELP)
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
    session_run_id: int | None = Field(
        None,
        description="a chat run's session: the id of its first run, which its branch chat/<id> names (the run's own id "
        "for the first); null for any other run",
    )
    session_turn: int | None = Field(
        None,
        description="a chat run's turn in its session: 1 for its first run, one more for each after it; null for any "
        "other run",
    )
    session_turns: int | None = Field(None, description="the runs of a chat run's session so far; null for another run")
    session_title: str | None = Field(
        None,
        description="the title of a chat run's session, its first run's, which the pull requests of its branch take; "
        "each run after it is titled by the message that woke it. Null for any other run",
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
    principal: str | None = Field(
        None,
        description="the login of the person the run acts for, whose autonomy bounds it (plan autonomy-levels); null "
        "for a local run and one queued before the hub knew principals",
    )
    autonomy: RunAutonomy | None = Field(None, description="the autonomy it acts under; null where principal is")
    as_owner: bool = Field(
        False,
        description="its owner lets its agent call the hub's API as them, secrets, tokens and workers aside, through a "
        "token of the run's own while a worker holds it (PUT .../runs/{id}/access changes it)",
    )
    as_owner_since: datetime | None = Field(
        None,
        description="when its owner let it run as them: at dispatch, or later through PUT .../access or the answer to "
        "a credential request; the run that resumes a parked run, and the next attempt of a lost one, keep the time of "
        "the run before it. Null while it does not run as its owner",
    )
