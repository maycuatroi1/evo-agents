"""The values of the reads of API 1.3 on a project's tasks, sprints, the people and the work of each task, the
changes of plan steps, how a reader sees labelled data, and whom a call of a plugin's tool acts for."""

from __future__ import annotations

import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from evo_agents.hub.access import ProjectRules
from evo_agents.hub.server.core_plugin_api.models.types import Session


@dataclass(frozen=True)
class TaskDetailRef:
    """A task with what ``TaskRef`` leaves out: its texts, its sprint's name, the key of what made it outside the
    hub and whether it is a task of an SRS frame."""

    id: int
    number: int
    type: str
    title: str
    status: str
    priority: str | None
    due_on: Any  # a date, or None
    sprint_id: int | None
    sprint: str | None  # the sprint's name
    parent_id: int | None
    description_md: str | None
    outcome_md: str | None
    external_key: str | None
    frame: bool  # made by the frame of an SRS document
    label: Mapping[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SprintDetailRef:
    """A sprint with its times: made, activated and closed."""

    id: int
    name: str
    goal: str | None
    state: str
    starts_on: Any
    ends_on: Any
    created_at: datetime
    activated_at: datetime | None
    closed_at: datetime | None


@dataclass(frozen=True)
class TaskPersonRef:
    task_id: int
    role: str  # assignee, qa, ... as the plugin tasks names them
    user_id: int
    login: str


@dataclass(frozen=True)
class TaskEventRef:
    id: int
    task_id: int
    field: str
    from_value: str | None
    to_value: str | None
    at: datetime


@dataclass(frozen=True)
class TaskActivityRef:
    """The latest comment on a task and the latest move of a run of it: one it links, or one of a plan step it
    links (queued, leased, started or finished)."""

    task_id: int
    commented_at: datetime | None
    run_at: datetime | None


@dataclass(frozen=True)
class TaskBlockerRef:
    """An open blocker of a task, set by hand: what it waits on is a member, a task (by number) or a text."""

    id: int
    task_id: int
    reason: str
    since: datetime
    set_by: str  # the login of who set it
    waiting_login: str | None
    waiting_task_number: int | None
    waiting_text: str | None


@dataclass(frozen=True)
class TaskDecisionRef:
    """A decision of a run of a task (one it links, or one of a plan step it links), and the member who
    dispatched the run."""

    task_id: int
    decision_id: int
    run_id: int
    question: str
    state: str
    asked_at: datetime
    dispatched_by: str  # their login


@dataclass(frozen=True)
class TaskSrsItemRef:
    """An SRS item a task links, with its title at the newest version of its document and its document's label."""

    task_id: int
    item_id: int
    document: str  # its document's slug
    kind: str
    code: str | None
    title: str | None
    retired_in: int | None
    label: Mapping[str, Any]  # its document's


@dataclass(frozen=True)
class PlanStepChangeRef:
    """The status of a plan's step changed from one revision to the next: ``revision`` made the change."""

    plan_id: str
    step_key: str
    revision: int
    changed_at: datetime  # when that revision was made
    from_status: str | None
    to_status: str | None


@dataclass(frozen=True, eq=False)
class Visibility:
    """How a reader with a grant up to ``max_level`` sees the labelled data of ``project`` through ``sink``: the one
    the read names, else the project's hub sink (None: neither, and nothing is visible). ``visible(label)`` is the
    read rule of the hub's routes for an object carrying ``label`` as stored."""

    project: str
    levels: tuple[str, ...]  # the project's ladder, lowest first
    max_level: str | None
    sink: str | None

    def visible(self, label) -> bool:
        rules = RULES.get(self)
        if rules is None:
            raise ValueError("a Visibility comes from read_visibility only")
        return self.sink is not None and rules.visible(label, self.max_level, self.sink)


@dataclass(frozen=True)
class ToolCaller:
    """Whom a call of one of the plugin's tools acts for: ``session`` is the member (None: nobody), and for the agent
    of a run, ``run_id`` and the run's kind."""

    session: Session | None
    run_id: int | None = None
    run_kind: str | None = None


RULES: weakref.WeakKeyDictionary[Visibility, ProjectRules] = weakref.WeakKeyDictionary()  # read_visibility's
