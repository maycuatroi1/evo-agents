"""What a worker claims: the request to claim, and the spec of the run it takes with the tasks of its kind."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub import author, runs
from evo_agents.hub.server.runs.models import browser as browser_help
from evo_agents.hub.server.runs.models.attachment_parts import Attachment
from evo_agents.hub.server.runs.models.routing import RunRouting
from evo_agents.hub.server.runs.models.run_parts import (
    ClaimedBudget,
    LostAttempt,
    PlanCopy,
    PluginRuntime,
    ReleaseSpec,
    RunCard,
    RunEnvironment,
    RunOrigin,
    RunPack,
    RunProfile,
    RunRepo,
    RunSkill,
)


class ClaimRequest(BaseModel):
    wait_s: float = Field(
        runs.CLAIM_WAIT_SECONDS, ge=0, le=runs.CLAIM_WAIT_SECONDS, description="how long to wait for a run"
    )


class ReplayTask(BaseModel):
    """The task a replay run does again (``evo_agents.hub.replay``): a step of a plan done before, in its repo, from its
    base commit (null until a replay of it read it from its commits)."""

    __module__ = "evo_agents.hub.server.runs.models"  # its OpenAPI name, shared with replays.ReplayTask, is by module

    id: int
    plan_id: str
    step_key: str
    repo: str
    title: str | None
    what: str
    verify: list[str] = Field(description="the step's verify, one shell line: the worker runs it on each arm's work")
    commits: list[str] = Field(description="the commits the step made, as its evidence or a run of it named them")
    base_sha: str | None = Field(description="the first parent of the oldest of its commits; null: the worker finds it")


class ReplaySpec(BaseModel):
    """What the worker of a replay run is told besides the change's branch and head: the replay, its task, and the
    change's repo, whose harness files the two arms run with."""

    replay_id: int
    task: ReplayTask
    repo: str = Field(description="the change's repo")


class ProbeSpec(BaseModel):
    """What the worker of a probe run is told (``evo_agents.hub.server.probe_runs``): the probe, the change whose rule
    its arms put on and take off, and the sessions of each arm."""

    probe_run_id: int
    probe_id: int
    repo: str = Field(description="the repo of the probe's task")
    base_sha: str | None = Field(description="the task's base commit; null: the default branch's tip at made_at")
    made_at: datetime = Field(description="when the probe was made")
    task: str = Field(description="the only text the agent of a session is given")
    verify: str = Field(description="one shell line the worker runs on each session's work; never shown the agent")
    k: int = Field(description="the sessions of each arm")
    week: date = Field(description="the Monday of the ISO week the run measures")
    change_repo: str = Field(description="the repo of the change, which holds its rule; a plugin's probe: its repo")
    merge_sha: str | None = Field(description="the change's merge commit; null: its head against the default branch")
    plugin: str | None = Field(None, description="(plugin probe) the core plugin whose rule the arms put on and off")
    ref: str | None = Field(None, description="(plugin probe) the key of the plugin's rule")
    arms: dict[str, dict[str, str]] | None = Field(None, description="(plugin probe) on and off: path to text")


class HistorySpec(BaseModel):
    """What the worker of a history run is told (``evo_agents.hub.server.history``): how it reads the project's
    history in batches."""

    batch_items: int = Field(description="items one batch reads at most")
    batch_chars: int = Field(description="characters of excerpts one batch reads at most")
    batch_turns: int = Field(description="turns of the agent on one batch")
    naming_share: float = Field(description="the share of the run's caps kept for naming the clusters")


class CuratorSpec(BaseModel):
    """What the worker of a run of the Curator (``evo_agents.hub.server.changes``) is told: the run's role, the
    charter's protected paths its watchdog compares the worktrees with, and for a Builder, a Judge or a replay the
    change, its branch, its forge and its pull request; for a Judge the commit to judge, for a replay the change's
    head, its task and its own key (``evo_agents.hub.server.replays``), for a history run its batches and its own
    key (``evo_agents.hub.server.history``), and for a probe run its probe, the change's head and its own key
    (``evo_agents.hub.server.probe_runs``)."""

    role: Literal["reviewer", "builder", "judge", "replay", "history", "probe"]
    protected_paths: list[str] = Field(default_factory=list, description="globs of the charter, repo:glob for one repo")
    change_id: int | None = None
    branch: str | None = Field(None, description="curator/..., the one branch a Builder pushes")
    forge: Literal["github", "gitlab"] | None = Field(None, description="gitlab: the push opens the merge request")
    base_branch: str | None = Field(None, description="the default branch the pull request goes into, when known")
    head_sha: str | None = Field(None, description="a Judge's commit to judge; null: the branch's tip")
    pr_url: str | None = None
    merge_result: bool = Field(
        False,
        description="a Judge of a merge check: merge the current base into the head in a scratch worktree and run the "
        "checks there; a conflict fails it with needs a rebase",
    )
    judge_key: str | None = Field(
        None,
        description="a judge run's own key, for GET .../judge and POST .../verdict (X-Evo-Judge-Key); the daemon keeps "
        "it in memory alone, never in a file, an environment or a log line",
    )
    replay: ReplaySpec | None = Field(None, description="a replay run's replay and task; null for any other run")
    replay_key: str | None = Field(
        None,
        description="a replay run's own key, for POST .../replay (X-Evo-Replay-Key); the daemon keeps it as a judge "
        "run's",
    )
    history: HistorySpec | None = Field(None, description="a history run's batches; null for any other run")
    history_key: str | None = Field(
        None,
        description="a history run's own key, for POST .../history/next and .../history/report (X-Evo-History-Key); "
        "the daemon keeps it as a judge run's",
    )
    probe: ProbeSpec | None = Field(None, description="a probe run's probe and change; null for any other run")
    probe_key: str | None = Field(
        None,
        description="a probe run's own key, for POST .../probe (X-Evo-Probe-Key); the daemon keeps it as a judge run's",
    )


class RunSpec(BaseModel):
    id: int
    kind: runs.RunKind
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
    lost_attempt: LostAttempt | None = Field(
        None,
        description="a plan run's attempt before this one, which the hub lost on this same worker (it stopped "
        "extending the lease), on the same runtime: the worker goes on in its agent's session and worktrees when it "
        "still has both, else starts as for any other attempt; null for any other run",
    )
    runtime: str = Field(description="a runtime of the core, or <plugin>/<name>, run with plugin_runtime's declaration")
    model: str | None = Field(description="the model the dispatch asked for; null: the runtime's own choice")
    routing: RunRouting | None = Field(
        None,
        description="while the project's policy has routing on, for a run that names no model: the scorer that rates "
        "its difficulty on the worker before the agent starts, and the model of each difficulty; null otherwise",
    )
    mode: runs.Mode
    approval: runs.Approval
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
    criteria: list[str] | None = Field(
        None,
        exclude_if=lambda value: value is None,
        description="the ids of the criteria of the plan's acceptance that the step of a run of one step names: its "
        "result file proves each (proofs.RESULT_KEY), or the run fails; absent for any other kind, and for a step "
        "that names none",
    )
    session_per_step: bool = Field(
        False,
        description="a plan run to run with a new agent session for each step not done yet, each with its prompt "
        "and the handoff of the plan; false: one session does every step, with the prompt above",
    )
    keep_going: int = Field(
        0,
        ge=0,
        exclude_if=lambda value: not value,
        description="a plan run: the turns in a row its worker sends the agent back to the plan, in its session, when "
        "a turn ends with steps to do and no decision open, before the run pushes and fails with steps_left; a turn "
        "after which fewer steps are left starts the count again. Absent (0): such a turn ends the run done",
    )
    browser: bool = Field(False, description=browser_help.SPEC_HELP, exclude_if=browser_help.no_browser)
    browser_identities: list[int] = Field(
        [], description=browser_help.SPEC_IDENTITIES_HELP, exclude_if=browser_help.no_identities
    )
    profile: RunProfile | None = Field(
        None, description="the agent profile the run went under; null for a run from before schema 0030"
    )
    own_home: bool = Field(
        False,
        description="true for a run under a profile that is not its project's default: the agent of a claude-code "
        "run gets a Claude Code config dir of its own in the run's directory instead of the owner's",
    )
    account_pool: bool = Field(
        False,
        description="true for a run of an agent in pool mode: its model account is the lease's alone, from the agent's "
        "account pool; the daemon uses none of its own environment's nor of this machine's login, and copies none "
        "of the owner's",
        exclude_if=lambda value: not value,
    )
    owner_setup: bool = Field(
        False,
        description="true for a run of its project's default agent in pool mode: the agent of a claude-code run keeps "
        "the owner's Claude Code setup (CLAUDE.md, settings, skills, agents, commands, plugins) in its config dir of "
        "its own, never the owner's login",
        exclude_if=lambda value: not value,
    )
    origin: RunOrigin | None = Field(
        None,
        description="the person who dispatched the run, whom the worker names as co-author of the commits of a chat "
        "run and of a run under a profile that is not its project's default; null from a hub that does not send it",
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
    release: ReleaseSpec | None = Field(
        None, description="a release run's cycle, mode and start commit; null for any other run"
    )
    autonomy: RunCard | None = Field(
        None, description="the autonomy card its prompt opens with; null for a run without a snapshot of autonomy"
    )
    attachments: list[Attachment] = Field(
        default_factory=list,
        description="a chat run's: the files of the chat message that opened it (chat_message_id), whose bytes GET "
        "/v1/worker/runs/{id}/attachments/{aid}/content serves; empty for any other run",
    )
    chat_message_id: int | None = Field(
        None,
        description="a chat run's: the chat message that opened it, whose files the worker saves under "
        ".evo-run/inbox/chat-<id>/; null for any other run",
    )


class Claim(BaseModel):
    run: RunSpec | None = Field(description="null when the wait ended without a run; claim again")
