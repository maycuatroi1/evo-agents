"""The ``evo-tasks/1`` JSON an import reads once (``TasksFile``: its people, its sources' statuses mapped to the
project's, its sprints and its tasks with their slots and links, use cases named by code), and what reading it against
a project found (``Plan``: the tasks as they will be kept, ``Planned``, and the ``Problem`` of each, an error that
stops the import or a warning of what it leaves out). ``evo_agents.hub.server.tasks.service.imports`` reads and
applies it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from evo_agents.hub.server.tasks.models.rows import Target
from evo_agents.hub.server.tasks.models.values import SprintInput, TaskInput
from evo_agents.hub.tables_tasks import (
    MAX_DESCRIPTION_CHARS,
    MAX_GOAL_CHARS,
    MAX_SPRINT_NAME_CHARS,
    MAX_TITLE_CHARS,
    SPRINT_STATES,
)

FORMAT = "evo-tasks/1"
MAX_TASKS = 5_000
ERROR, WARNING = "error", "warning"
Key = Field(min_length=1, max_length=200)


class TasksFilePerson(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str = Key
    name: str = Field("", max_length=200)
    github: str | None = Field(
        None, max_length=100, description="the GitHub login; none leaves the person's slots empty"
    )


class TasksFileSprint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str = Key
    name: str = Field(min_length=1, max_length=MAX_SPRINT_NAME_CHARS)
    goal: str = Field("", max_length=MAX_GOAL_CHARS)
    starts_on: date | None = None
    ends_on: date | None = None
    state: Literal[SPRINT_STATES] = "planned"  # pyright: ignore[reportInvalidTypeForm]


class TasksFileExternal(BaseModel):
    model_config = ConfigDict(extra="ignore")

    system: str = Field(min_length=1, max_length=50)
    id: str = Field(min_length=1, max_length=200)
    url: str | None = Field(None, max_length=2000)


class TasksFileStep(BaseModel):
    plan: str = Field(min_length=1, max_length=100)
    step: str = Field(min_length=1, max_length=100)


class TasksFileRoles(BaseModel):
    ba: list[str] = Field(default_factory=list, max_length=50)
    qa: list[str] = Field(default_factory=list, max_length=50)
    dev: list[str] = Field(default_factory=list, max_length=50)


class TasksFileTask(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str = Key
    external: TasksFileExternal | None = None
    type: str | None = Field(None, max_length=40, description="one of the project's types; its first when left out")
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    description_md: str = Field("", max_length=MAX_DESCRIPTION_CHARS)
    status: str | None = Field(None, max_length=100, description="a status of the source, mapped by status_map")
    priority: str = Field("medium", max_length=20)
    due_on: date | None = None
    done_on: date | None = None
    sprint: str | None = Field(None, max_length=200, description="the key of a sprint of the file")
    parent: str | None = Field(None, max_length=200, description="the key of another task of the file")
    assignees: list[str] = Field(default_factory=list, max_length=50, description="keys of people of the file")
    roles: TasksFileRoles = Field(default_factory=TasksFileRoles)
    use_cases: list[str] = Field(default_factory=list, max_length=100, description="codes of SRS items")
    plan_steps: list[TasksFileStep] = Field(default_factory=list, max_length=100)
    pulls: list[str] = Field(default_factory=list, max_length=100)


class TasksFile(BaseModel):
    """Tasks to import once, in the format evo-tasks/1."""

    model_config = ConfigDict(extra="ignore")

    format: Literal[FORMAT]  # pyright: ignore[reportInvalidTypeForm]
    people: list[TasksFilePerson] = Field(default_factory=list, max_length=MAX_TASKS)
    status_map: dict[str, str] = Field(default_factory=dict, description="a status of the source -> a status key")
    sprints: list[TasksFileSprint] = Field(default_factory=list, max_length=500)
    tasks: list[TasksFileTask] = Field(max_length=MAX_TASKS)


@dataclass(frozen=True)
class Problem:
    level: str
    message: str
    task: str | None = None

    def as_json(self) -> dict:
        return {"level": self.level, "task": self.task, "message": self.message}


@dataclass
class Planned:
    """A task of the file as it will be kept."""

    key: str
    task: TaskInput
    targets: list[Target]
    parent: str | None
    done_on: date | None


@dataclass
class Plan:
    tasks: list[Planned] = field(default_factory=list)
    sprints: list[SprintInput] = field(default_factory=list)
    sprint_names: dict[str, str] = field(default_factory=dict)  # key -> name
    skipped: list[tuple[str, int]] = field(default_factory=list)  # key -> number of the task imported before
    problems: list[Problem] = field(default_factory=list)

    @property
    def refused(self) -> bool:
        return any(problem.level == ERROR for problem in self.problems)

    def links(self) -> int:
        return sum(len(item.targets) for item in self.tasks)


__all__ = [
    "ERROR",
    "FORMAT",
    "MAX_TASKS",
    "WARNING",
    "Plan",
    "Planned",
    "Problem",
    "TasksFile",
    "TasksFileExternal",
    "TasksFilePerson",
    "TasksFileRoles",
    "TasksFileSprint",
    "TasksFileStep",
    "TasksFileTask",
]
