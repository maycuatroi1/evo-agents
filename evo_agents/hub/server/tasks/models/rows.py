"""What the repository reads of the plugin tasks, as values: a task (``TaskRow``) and who stands in its slots
(``People``), its history (``EventRow``) and comments (``CommentRow``), a sprint (``SprintRow``), a member a task may
name (``MemberRow``), a blocker (``BlockerRow``) and what it waits on (``Waiting``), a link's target (``Target``) and a
link as a task shows it (``LinkView``, with a plan step as the plan holds it now, ``StepState``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from evo_agents.hub.server.tasks.models.values import SLOTS

MANUAL, DECISION = "manual", "decision"
KINDS = (MANUAL, DECISION)  # of a blocker: set by hand, or an open decision of a run of the task
WAITING_KINDS = ("member", "task", "text")
DONE = "done"  # a plan step's status once it is finished


@dataclass(frozen=True)
class TaskRow:
    """A task as its row and joins give it."""

    id: int
    number: int
    type: str
    title: str
    description_md: str
    outcome_md: str
    status: str
    priority: str
    due_on: date | None
    sprint: str | None
    parent_number: int | None
    label: dict
    external_ref: dict | None
    created_by: str
    created_at: datetime
    updated_at: datetime


@dataclass
class People:
    """Who stands in each slot of a task, by login."""

    assignees: list[str] = field(default_factory=list)
    roles: dict[str, list[str]] = field(default_factory=lambda: {slot: [] for slot in SLOTS})

    def logins(self) -> set[str]:
        return set(self.assignees).union(*self.roles.values())


@dataclass(frozen=True)
class EventRow:
    actor: str | None
    field: str
    from_value: str | None
    to_value: str | None
    cause: str
    at: datetime


@dataclass(frozen=True)
class CommentRow:
    id: int
    author: str
    body: str
    created_at: datetime


@dataclass(frozen=True)
class SprintRow:
    id: int
    name: str
    goal: str
    starts_on: date | None
    ends_on: date | None
    state: str
    created_by: str
    created_at: datetime
    closed_at: datetime | None
    activated_at: datetime | None
    tasks: int


@dataclass(frozen=True)
class MemberRow:
    """A member of the project, whom a task may name, with the role of their grant."""

    login: str
    role: str


@dataclass(frozen=True)
class BlockerRow:
    """A blocker as it is read: ``id`` is the row's for a blocker set by hand, None for an open decision."""

    id: int | None
    kind: str
    reason: str
    waiting_kind: str
    waiting_on: str
    since: datetime
    set_by: str | None
    cleared_at: datetime | None = None
    cleared_by: str | None = None
    decision_id: int | None = None
    run_id: int | None = None


@dataclass
class Waiting:
    """Who or what a blocker waits for: exactly one of the three is given."""

    member: str | None = None
    task: str | None = None
    text: str | None = None

    def given(self) -> int:
        return sum(value is not None for value in (self.member, self.task, self.text))


@dataclass(frozen=True)
class Target:
    """A link's target, its columns as tasks_links holds them."""

    kind: str
    srs_item_id: int | None = None
    plan_id: str | None = None
    step_id: str | None = None
    run_id: int | None = None
    url: str | None = None
    name: str | None = field(default=None, compare=False)  # how the history names it

    def columns(self) -> dict:
        return {
            "kind": self.kind,
            "srs_item_id": self.srs_item_id,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "run_id": self.run_id,
            "url": self.url,
        }


@dataclass(frozen=True)
class StepState:
    """A plan's step as the hub holds it now: None for what the member cannot see or the plan no longer has."""

    title: str | None
    status: str | None
    hidden: bool = False


@dataclass
class LinkView:
    """A link as a task shows it."""

    id: int
    kind: str
    created_by: str
    created_at: datetime
    hidden: bool = False
    code: str | None = None
    item_kind: str | None = None
    item_title: str | None = None
    document: str | None = None
    retired: bool = False
    plan_id: str | None = None
    step: str | None = None
    step_title: str | None = None
    step_status: str | None = None
    runs: list[dict] = field(default_factory=list)
    run_id: int | None = None
    run_kind: str | None = None
    run_state: str | None = None
    url: str | None = None


__all__ = [
    "DECISION",
    "DONE",
    "KINDS",
    "MANUAL",
    "WAITING_KINDS",
    "BlockerRow",
    "CommentRow",
    "EventRow",
    "LinkView",
    "MemberRow",
    "People",
    "SprintRow",
    "StepState",
    "Target",
    "TaskRow",
    "Waiting",
]
