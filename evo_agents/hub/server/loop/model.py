"""The Loop's words and shapes (plan loop-engine, contract loop/1): the kinds, cycles, states and reasons of a work
item, the kinds of its ledger, and what an engine reads and returns. Pure: no query, no write.

An engine sits behind ``Engine``, three functions that read and never write:

- ``rules(event, view)``: what one event of hub_events makes, each a ``WorkSpec`` (a work item that should exist, made
  once by its key), a ``Transition`` (an existing work item moves) or a ``Note`` (a result written into the chain of a
  claim);
- ``scheduled(view)``: what the clock makes, read on each tick (a goal's new revision, the weekly look at each goal);
- ``reconcile(item, view)``: where an unfinished work item moves now, from what the view holds of its ancestors and
  of the run, plan and merge requests it waits on; None while it stays.

The tick (``evo_agents.hub.server.loop.tick``) builds the view, calls the engine and writes what it returns. Changing
the engine changes the code behind these three functions alone: the rows of loop/1 read the same, and a new engine
goes on from the cursor with the same keys, so it makes no work item again.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

ENGINE = "rules/1"  # the engine of this release: the table of rules of the report's section Loop

KINDS = ("brainstorm", "use_case", "build", "deliver")
BRAINSTORM, USE_CASE, BUILD, DELIVER = KINDS
CYCLES = ("business", "development")
BUSINESS, DEVELOPMENT = CYCLES
CYCLE_OF = {BRAINSTORM: BUSINESS, USE_CASE: BUSINESS, BUILD: DEVELOPMENT, DELIVER: DEVELOPMENT}

STATES = ("queued", "running", "watching", "waiting_human", "done", "cancelled")
QUEUED, RUNNING, WATCHING, WAITING_HUMAN, DONE, CANCELLED = STATES
FINISHED = (DONE, CANCELLED)  # tables_loop.FINISHED
# Why a work item stands where it does, by state; done has no reason.
REASONS = {
    QUEUED: ("breaker", "concurrency", "rate", "worker", "type_refused"),
    RUNNING: (),
    WATCHING: ("stable", "auto_tasks", "split", "merge_queue", "triage"),
    WAITING_HUMAN: (
        "srs_review",
        "srs_finding",
        "srs_changes",
        "author_question",
        "run_decision",
        "loop_decision",
        "merge_blocked",
        "merge_manual",
    ),
    DONE: (),
    CANCELLED: (
        "claim_invalidated",
        "claim_retired",
        "goal_paused",
        "goal_closed",
        "loop_off",
        "task_closed",
        "no_frame_task",
        "person",
        "dropped",
        "manual",
    ),
}
ALL_REASONS = tuple(reason for reasons in REASONS.values() for reason in reasons)
# The ledger of a work item (loop_item_events.kind)
EVENT_KINDS = (
    "created",
    "ready",
    "admitted",
    "dispatched",
    "waiting",
    "failed",
    "triaged",
    "asked",
    "done",
    "cancelled",
    "noted",
)
CREATED, READY, ADMITTED, DISPATCHED, WAITING, FAILED, TRIAGED, ASKED, DONE_EVENT, CANCELLED_EVENT, NOTED = EVENT_KINDS
STRATEGIST, BA = "@strategist", "@ba"  # who a business work item stands for: the agent types of canvas and srs
OWNER_OF = {BRAINSTORM: STRATEGIST, USE_CASE: BA}

# Claims, goals, tasks and runs as the engine reads them
TESTING, VALIDATED, INVALIDATED, RETIRED = "testing", "validated", "invalidated", "retired"
ENDED_CLAIMS = (INVALIDATED, RETIRED)  # a claim whose chain the Loop cancels
ACTIVE, PAUSED, CLOSED = "active", "paused", "closed"  # goals.STATUSES
TODO, DOING, DONE_GROUP, CANCELLED_GROUP = "todo", "doing", "done", "cancelled"  # groups of the plugin tasks
CLOSED_GROUPS = (DONE_GROUP, CANCELLED_GROUP)
RUN_ENDED = ("done", "failed", "lost", "cancelled")  # runs.TERMINAL_STATES
MERGE_FINAL = "done"  # merge_gate.STATES: what follows its merge is over
MERGED, REVERTED = "merged", "reverted"
MERGE_HELD = ("blocked", "paused")  # merge_gate.STATES: a merge request that waits for a person
ASKING = ("waiting", "parked")  # the states of a run whose agent waits for its owner's answer
DRAFT, IN_REVIEW, WITHDRAWN = "draft", "in_review", "withdrawn"  # tables_srs.VERSION_STATES
# Triage and the person (the report's section Triage): the failure that opens triage, the one that asks a person, and
# how long the third hand-out waits at most for the chat run of the triage thread.
TRIAGE_AT, ASK_AT = 2, 3
TRIAGE_WAIT_MINUTES = 60


@dataclass(frozen=True)
class Event:
    """A row of hub_events."""

    id: int
    kind: str
    payload: dict
    created_at: datetime


@dataclass(frozen=True)
class GoalView:
    """A goal at its newest revision: ``reached`` is whether its metric meets its target now (None: no metric, or no
    signal in its window)."""

    id: int
    code: str
    revision: int
    status: str
    has_metric: bool
    reached: bool | None
    canvas_id: int | None
    rank: int
    owner_id: int


@dataclass(frozen=True)
class ClaimView:
    """A claim of a canvas, its status as the newest approved baseline holds it (None before one holds it)."""

    id: int
    canvas_id: int
    canvas: str
    code: str
    status: str | None
    use_cases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ItemView:
    """A work item as the engine reads it; ``runs`` holds every run handed out for it."""

    id: int
    kind: str
    key: str
    state: str
    reason: str | None
    goal_id: int | None
    claim_id: int | None = None
    srs_document_id: int | None = None
    use_case: str | None = None
    srs_version: int | None = None
    task_id: int | None = None
    plan_id: str | None = None
    run_id: int | None = None
    parent_id: int | None = None
    detail: Mapping = field(default_factory=dict)
    runs: tuple[int, ...] = ()
    attempts: int = 0
    failures: int = 0

    @property
    def finished(self) -> bool:
        return self.state in FINISHED


@dataclass(frozen=True)
class TaskView:
    """A frame task: its group by the config of the plugin tasks (None for a status the config lacks), whether it is
    in an active sprint, and whether it is open (in no group done or cancelled)."""

    id: int
    number: int
    group: str | None
    sprint_active: bool = False

    @property
    def open(self) -> bool:
        return self.group not in CLOSED_GROUPS


@dataclass(frozen=True)
class ApprovalView:
    """What the plugin srs did of an approved version (srs_approval_tasks): the split run it handed out, whether that
    ended (true when there is none)."""

    version_id: int
    split_run_id: int | None
    split_ended: bool


@dataclass(frozen=True)
class RunView:
    """A run a work item waits on, the newest of those that go on from the run handed out for it (a run resumed, the
    next attempt of a run lost): its kind and state, the plan an author run wrote (None: none), for a plan run whether
    every step of its plan is done and the repos it works in, whether its owner's end of an author run's chat was asked
    (``finish_requested``), its decisions open, and the proposals of claims it made."""

    id: int
    kind: str
    state: str
    plan_id: str | None = None
    steps_done: bool | None = None
    repos: tuple[str, ...] = ()
    finish_requested: bool = False
    decisions: int = 0
    proposals: int = 0

    @property
    def ended(self) -> bool:
        return self.state in RUN_ENDED

    @property
    def asks(self) -> bool:
        """Whether its agent waits for its owner: a turn ended with a question, or a decision open."""
        return self.state in ASKING and not self.finish_requested


@dataclass(frozen=True)
class VersionView:
    """The version of an SRS document a use_case work item's runs drafted, as it stands now: the newest version of that
    document from the first its runs drafted on (a settle chat may write the next); the ids of the open findings of
    level decide of the document (``decide``), the link of the version's settle chat, and whether a reviewer asked for
    changes in its last round (``changes``)."""

    id: int
    document_id: int
    document: str
    number: int
    state: str
    decide: tuple[int, ...] = ()
    chat: str | None = None
    changes: bool = False


@dataclass(frozen=True)
class MergeView:
    """A merge request of the merge queue of a plan; ``checked`` when its repo names a UAT or PROD check job."""

    id: int
    repo: str
    state: str
    after_sha: str | None
    merged_at: datetime | None
    checked: bool


@dataclass(frozen=True)
class View:
    """What one tick reads of a project. ``week`` is the ISO week whose weekly look at goals has come, at ``week_at``
    (None before it comes this week); ``keys`` the keys of the project's brainstorm work items; ``brainstormed`` when
    each goal's newest brainstorm was made; ``proposals`` the open proposals of claims a goal's brainstorm runs made;
    ``versions`` the run that drafted each version an event names; ``tasks`` the frame tasks of the work items by id
    and ``frames`` by external key; ``approvals`` what the plugin srs did of each approved version, by its id;
    ``runs`` the runs the work items wait on by the id handed out (the triage chat runs by their own); ``drafts`` the
    version each use_case work item drafted, by work item; ``manual`` the repos whose merge requests the hub's queue
    does not merge (``repos.<repo>.mode`` off, or on GitHub); ``released`` the repos of each deliver's plan its
    ``repos`` mark merged (or not needed)."""

    now: datetime
    policy: Mapping
    goals: Mapping[int, GoalView] = field(default_factory=dict)
    claims: Mapping[int, ClaimView] = field(default_factory=dict)
    items: tuple[ItemView, ...] = ()
    keys: frozenset[str] = frozenset()
    week: str | None = None
    week_at: datetime | None = None
    brainstormed: Mapping[int, datetime] = field(default_factory=dict)
    proposals: Mapping[int, int] = field(default_factory=dict)
    versions: Mapping[int, int | None] = field(default_factory=dict)
    tasks_on: bool = False
    tasks: Mapping[int, TaskView] = field(default_factory=dict)
    frames: Mapping[str, TaskView] = field(default_factory=dict)
    approvals: Mapping[int, ApprovalView] = field(default_factory=dict)
    runs: Mapping[int, RunView] = field(default_factory=dict)
    merges: Mapping[str, tuple[MergeView, ...]] = field(default_factory=dict)
    drafts: Mapping[int, VersionView] = field(default_factory=dict)
    manual: frozenset[str] = frozenset()
    released: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def goal_of_canvas(self, canvas_id: int) -> GoalView | None:
        """The active goal with a metric that a claim of canvas ``canvas_id`` serves: the first by rank among those
        naming the canvas, else the project's one active goal with a metric when it names no canvas; None otherwise."""
        working = sorted(
            (goal for goal in self.goals.values() if goal.status == ACTIVE and goal.has_metric),
            key=lambda goal: (goal.rank, goal.id),
        )
        named = [goal for goal in working if goal.canvas_id == canvas_id]
        if named:
            return named[0]
        if len(working) == 1 and working[0].canvas_id is None:
            return working[0]
        return None

    def chain(self, claim_id: int) -> list[ItemView]:
        """The work items of claim ``claim_id``, oldest first."""
        return sorted((item for item in self.items if item.claim_id == claim_id), key=lambda item: item.id)


@dataclass(frozen=True)
class WorkSpec:
    """A work item that should exist; the tick makes it unless its key is taken. ``parent_id`` names the work item it
    comes of."""

    kind: str
    key: str
    goal_id: int | None
    state: str = QUEUED
    reason: str | None = None
    claim_id: int | None = None
    srs_document_id: int | None = None
    use_case: str | None = None
    srs_version: int | None = None
    task_id: int | None = None
    plan_id: str | None = None
    parent_id: int | None = None
    owner: str | None = None
    detail: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class Transition:
    """Work item ``item_id`` moves to ``state`` with ``reason``, writing a ledger row of kind ``event``. ``detail`` is
    merged into its detail; ``task_id`` and ``plan_id`` fill its ancestors; ``failed`` counts a failure; ``spawn`` are
    the work items that come of it."""

    item_id: int
    state: str
    event: str
    reason: str | None = None
    detail: Mapping = field(default_factory=dict)
    task_id: int | None = None
    plan_id: str | None = None
    failed: bool = False
    spawn: tuple[WorkSpec, ...] = ()


@dataclass(frozen=True)
class Note:
    """A result written once into the chain of claim ``claim_id`` (``key``): a ledger row ``noted`` on its newest work
    item, and a comment of ``author_id`` on each task of ``comment_on`` when ``comment`` is given."""

    claim_id: int
    key: str
    detail: Mapping
    comment: str | None = None
    comment_on: tuple[int, ...] = ()
    author_id: int | None = None


Effect = WorkSpec | Transition | Note


class Engine(Protocol):
    """What the tick calls; nothing behind it writes."""

    name: str

    def rules(self, event: Event, view: View) -> list[Effect]: ...

    def scheduled(self, view: View) -> list[Effect]: ...

    def reconcile(self, item: ItemView, view: View) -> Transition | None: ...
