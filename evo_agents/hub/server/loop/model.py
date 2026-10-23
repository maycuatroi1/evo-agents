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

The engine of this release is ``rules/2`` (plan problem-oriented-hub, criterion a6; ``rules2``), which goes from the
problem a goal answers: each work item it makes carries that problem (``problem_id``), a claim of the cells Solution
and Unique value proposition moved to testing makes a ``use_case`` only in a project of use cases (``loop.use_cases``,
``View.use_cases``) and a ``build`` straight from the claim otherwise, and a claim of the Problem cell makes a
``discover``. ``rules/1`` (``rules``) stays, and its work items read and move as before. Everything here was added to
loop/1: no word of it changed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

ENGINES = ("rules/1", "rules/2")  # loop_items.engine of the core work items, oldest first
RULES_1, RULES_2 = ENGINES
ENGINE = RULES_2  # the engine of this release (plan problem-oriented-hub): it goes from the problem a goal answers

KINDS = ("brainstorm", "use_case", "build", "deliver", "discover")
BRAINSTORM, USE_CASE, BUILD, DELIVER, DISCOVER = KINDS
CYCLES = ("business", "development")
BUSINESS, DEVELOPMENT = CYCLES
CYCLE_OF = {BRAINSTORM: BUSINESS, USE_CASE: BUSINESS, BUILD: DEVELOPMENT, DELIVER: DEVELOPMENT, DISCOVER: BUSINESS}
# The kinds a core plugin's Loop rules make (extension point loop_rules): of cycle business, so they count against
# ``loop.business.max_running`` with the core work items of that cycle. The routes and the web know the core kinds of
# KINDS alone: a work item of these kinds is read through its plugin until the Loop's pages show them.
PLUGIN_KINDS = ("growth_idea", "growth_draft", "growth_publish", "growth_invite", "growth_review")
CYCLE_OF.update(dict.fromkeys(PLUGIN_KINDS, BUSINESS))
PLUGIN_OFF = "plugin_off"  # the cancelled reason of a plugin's work item whose plugin is off in the project
PLUGIN_ENGINE = "plugin/{}"  # loop_items.engine of the work items a plugin's rules made

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
OWNER_OF = {BRAINSTORM: STRATEGIST, USE_CASE: BA, DISCOVER: STRATEGIST}
# rules/2: the cells of the canvas whose claims a baseline moved to testing make core work items (tables_canvas.CELLS);
# a claim of any other cell makes none, whatever a core plugin's rule makes of it
PROBLEM_CELL = "problem"
SOLUTION_CELLS = ("solution", "unique_value_proposition")
# loop.use_cases: always and never fix whether a solution goes through use cases of SRS documents; auto does when the
# plugin srs is on in the project and it has an SRS document not archived
USE_CASE_MODES = ("always", "never", "auto")
ALWAYS, NEVER, AUTO_MODE = USE_CASE_MODES
CLAIM_BUILD = "build:claim:"  # the start of the key of a build rules/2 made straight from a claim

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
    signal in its window); ``problem_id`` the problem of the project it answers (plan problem-oriented-hub), None for
    none."""

    id: int
    code: str
    revision: int
    status: str
    has_metric: bool
    reached: bool | None
    canvas_id: int | None
    rank: int
    owner_id: int
    problem_id: int | None = None


@dataclass(frozen=True)
class ClaimView:
    """A claim of a canvas, its status as the newest approved baseline holds it (None before one holds it), and its
    cell (None: not read)."""

    id: int
    canvas_id: int
    canvas: str
    code: str
    status: str | None
    use_cases: tuple[str, ...] = ()
    cell: str | None = None


@dataclass(frozen=True)
class ItemView:
    """A work item as the engine reads it; ``runs`` holds every run handed out for it, ``problem_id`` the problem its
    goal answered when it was made (None: none, or made before rules/2)."""

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
    problem_id: int | None = None

    @property
    def finished(self) -> bool:
        return self.state in FINISHED

    @property
    def from_claim(self) -> bool:
        """Whether it is a build rules/2 made straight from a claim, with no use case and no frame task."""
        return self.kind == BUILD and self.key.startswith(CLAIM_BUILD)


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
    """A merge request of the merge queue of a plan; ``checked`` when its repo names a UAT or PROD check job;
    ``outside`` when the queue closed it because GitLab says a person merged it (its ledger's line ``closed``), its
    ``after_sha`` and ``merged_at`` then the merge GitLab named and when the queue saw it."""

    id: int
    repo: str
    state: str
    after_sha: str | None
    merged_at: datetime | None
    checked: bool
    outside: bool = False


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
    ``repos`` mark merged (or not needed); ``use_cases`` whether a solution goes through use cases of SRS documents in
    the project (``loop.use_cases``, read by rules/2)."""

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
    use_cases: bool = True

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
    comes of, ``problem_id`` the problem its goal answers."""

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
    problem_id: int | None = None


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
