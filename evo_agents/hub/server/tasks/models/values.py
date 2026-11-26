"""The values every layer of the plugin tasks shares: the refusal each raises (``TasksError``), the plugin's effective
config in a project (``Settings``) and how a task is named (``number_of``), who writes (``Writer``), what a write
gives (``TaskInput``, ``TaskChanges``, ``LinksIn``, ``SprintInput``), what a list keeps (``Filters``), and what a
member reads through (``sink_of``, ``visible``).

A task carries a label (the project's default, or one an admin set), and it is read under that label: through the
sink X-Evo-Sink names, else the project's hub sink, as plans are read (``ProjectAccess.visible``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.tasks import FINISHED
from evo_agents.hub.tables_tasks import PEOPLE_ROLES

PLUGIN = "tasks"
SRS = "srs"  # the plugin whose items a task links to, while the project enables it
REF = re.compile(r"^(?:([A-Za-z][A-Za-z0-9]{0,9})-)?([1-9][0-9]{0,8})$")  # T-42, or 42
SINK_HEADER = "X-Evo-Sink"  # evo_agents.hub.server.plans.SINK_HEADER
SLOTS = PEOPLE_ROLES[1:]  # the roles of a task beside its assignees: ba, qa, dev
MAX_LISTED = 2_000
MAX_DEPTH = 50  # of a chain of parents


class TasksError(Exception):
    """Why the hub refuses a request about tasks, with the HTTP status a route answers and any detail (the problems
    of an import); nothing was written."""

    def __init__(self, status: int, message: str, detail: list | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail


@dataclass(frozen=True)
class Status:
    key: str
    name: str
    group: str


@dataclass(frozen=True)
class Settings:
    """The plugin's effective config in a project."""

    prefix: str
    statuses: tuple[Status, ...]
    types: tuple[str, ...]
    on_steps_done: str | None = None

    def status(self, key: str | None) -> Status | None:
        return next((status for status in self.statuses if status.key == key), None)

    def group(self, key: str | None) -> str | None:
        found = self.status(key)
        return found.group if found else None

    def finished(self, key: str | None) -> bool:
        return self.group(key) in FINISHED

    def ref(self, number: int) -> str:
        """A task's number as shown: T-42."""
        return f"{self.prefix}-{number}"


def number_of(ref: str) -> int:
    """The number a task is named by: ``T-42`` (any prefix, the project's or one it had) or ``42``; 404 otherwise."""
    found = REF.fullmatch(ref.strip())
    if found is None:
        raise TasksError(404, f"{ref!r} names no task: give its number, as T-42 or 42")
    return int(found[2])


def sink_of(access: ProjectAccess, sink: str | None) -> str | None:
    """The sink a read goes through: the one named, else the project's hub sink."""
    if sink:
        return sink
    return access.rules.hub_sink.sink if access.rules.hub_sink else None


def visible(access: ProjectAccess, label, sink: str | None) -> bool:
    through = sink_of(access, sink)
    return through is not None and access.visible(label, through)


@dataclass(frozen=True)
class Writer:
    """Who writes, where, and through which sink they read what they link to."""

    access: ProjectAccess
    user_id: int
    settings: Settings
    sink: str | None = None


@dataclass(frozen=True)
class Filters:
    """What a list of tasks keeps: a status, a sprint by name (or the tasks of none), the tasks a member stands in a
    slot of, the tasks an assignee has, the tasks that are blocked."""

    status: str | None = None
    sprint: str | None = None
    backlog: bool = False
    member_id: int | None = None
    assignee: str | None = None
    limit: int = 500
    blocked: bool = False


@dataclass
class LinksIn:
    """Links to add, as a member names their targets."""

    use_cases: list[str] = field(default_factory=list)  # codes of SRS items
    steps: list[str] = field(default_factory=list)  # plan#step
    runs: list[int] = field(default_factory=list)
    pulls: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.use_cases or self.steps or self.runs or self.pulls)


@dataclass
class TaskInput:
    """A new task. ``type`` and ``status`` take the config's first when left out."""

    title: str
    description_md: str = ""
    outcome_md: str = ""
    type: str | None = None
    status: str | None = None
    priority: str = "medium"
    due_on: date | None = None
    sprint: str | None = None
    parent: str | None = None
    assignees: list[str] = field(default_factory=list)
    roles: dict[str, list[str]] = field(default_factory=dict)
    links: LinksIn = field(default_factory=LinksIn)
    label: dict | None = None
    external_key: str | None = None
    external_ref: dict | None = None


@dataclass
class TaskChanges:
    """A change of a task: the fields ``values`` sets (``due_on``, ``sprint`` and ``parent`` None to clear), the
    assignees when given (the whole list), each role given (its whole list), links to add and link ids to drop."""

    values: dict = field(default_factory=dict)
    assignees: list[str] | None = None
    roles: dict[str, list[str]] = field(default_factory=dict)
    links: LinksIn = field(default_factory=LinksIn)
    unlink: list[int] = field(default_factory=list)


@dataclass
class SprintInput:
    name: str
    goal: str = ""
    starts_on: date | None = None
    ends_on: date | None = None
    state: str = "planned"


__all__ = [
    "MAX_DEPTH",
    "MAX_LISTED",
    "PLUGIN",
    "SINK_HEADER",
    "SLOTS",
    "SRS",
    "Filters",
    "LinksIn",
    "Settings",
    "SprintInput",
    "Status",
    "TaskChanges",
    "TaskInput",
    "TasksError",
    "Writer",
    "number_of",
    "sink_of",
    "visible",
]
