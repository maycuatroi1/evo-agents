"""What the daily report is computed from: the project's tasks and everything the report reads of each, as plain
values. ``reads`` fills them from the hub's tables; ``report`` turns them, with a date, into the answer. Nothing here
touches a database or a clock, so a test builds the facts by hand and fixes the date.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time

from evo_agents.core_plugins.pm.workdays import Calendar

DONE, CANCELLED, TODO, DOING, REVIEW = "done", "cancelled", "todo", "doing", "review"
FINISHED = (DONE, CANCELLED)  # the groups of a task nobody works on any more
MANUAL, DECISION = "manual", "decision"  # the kinds of a blocker


@dataclass(frozen=True)
class Config:
    """The plugin's effective config in a project."""

    calendar: Calendar
    stale_days: int = 2
    escalate_after_days: int = 1
    at_risk_days: int = 2
    wip_limit: int = 2
    pm: str | None = None
    digest_chat: int | None = None
    digest_at: time = time(8, 0)


@dataclass(frozen=True)
class Event:
    """A row of a task's history."""

    field: str
    from_value: str | None
    to_value: str | None
    at: datetime


@dataclass(frozen=True)
class Blocker:
    """What a task waits on: a blocker set by hand, or an open decision of a run that belongs to the task."""

    kind: str
    reason: str
    waiting_kind: str
    waiting_on: str
    since: datetime
    set_by: str | None = None
    decision_id: int | None = None
    run_id: int | None = None
    blocker_id: int | None = None  # the row of tasks_blockers, for a blocker set by hand


@dataclass(frozen=True)
class UseCase:
    """A use case of the plugin srs that a task links to."""

    code: str
    title: str
    visible: bool = True


@dataclass(frozen=True)
class Task:
    ref: str
    number: int
    title: str
    status: str
    group: str
    created_at: datetime
    due_on: date | None = None
    sprint: str | None = None
    description_md: str = ""
    outcome_md: str = ""
    frame: bool = False  # the frame task an SRS approval made
    visible: bool = True  # whether the reader may see it; a task they may not still counts
    assignees: tuple[str, ...] = ()
    qa: tuple[str, ...] = ()
    events: tuple[Event, ...] = ()
    activity: dict[str, datetime] = field(default_factory=dict)  # source -> its latest time: comment, run, plan_step
    blockers: tuple[Blocker, ...] = ()
    use_cases: tuple[UseCase, ...] = ()

    @property
    def open(self) -> bool:
        return self.group not in FINISHED


@dataclass(frozen=True)
class Sprint:
    name: str
    created_at: datetime
    starts_on: date | None = None
    ends_on: date | None = None
    activated_at: datetime | None = None


@dataclass(frozen=True)
class Member:
    login: str
    role: str


@dataclass(frozen=True)
class Inputs:
    project: str
    config: Config
    groups: dict[str, str]  # a status key -> its group
    tasks: tuple[Task, ...]
    sprint: Sprint | None = None
    members: tuple[Member, ...] = ()
