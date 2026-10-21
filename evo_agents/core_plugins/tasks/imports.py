"""Importing tasks once from ``evo-tasks/1`` JSON (``TasksFile``): its people, its sources' statuses mapped to the
project's, its sprints and its tasks with their slots and links, use cases named by code.

``plan`` reads the file against the project and writes nothing: each task's status (through ``status_map``, else as
given) must be a status of the project and its type one of its types, its people members with a grant, its sprint
one of the file's or the project's, its links targets the member sees. What stops an import is an error; what it
leaves out is a warning: a person with no GitHub login or no grant, a use case code no SRS item holds (kept in the
task's ``external_ref``), a plan step that is not there, a parent the file does not have. A task whose source
(``external``: system and id) an import brought in already is skipped. ``apply`` writes what ``plan`` found, every
task numbered in the file's order, its done date a row of its history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.tasks import PRIORITIES, links, writes
from evo_agents.core_plugins.tasks.settings import TasksError
from evo_agents.core_plugins.tasks.store import SLOTS
from evo_agents.hub import tables
from evo_agents.hub.tables_tasks import (
    MAX_DESCRIPTION_CHARS,
    MAX_EXTERNAL_KEY_CHARS,
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
    state: Literal[SPRINT_STATES] = "planned"


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

    format: Literal[FORMAT]
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
    task: writes.TaskInput
    targets: list[links.Target]
    parent: str | None
    done_on: date | None


@dataclass
class Plan:
    tasks: list[Planned] = field(default_factory=list)
    sprints: list[writes.SprintInput] = field(default_factory=list)
    sprint_names: dict[str, str] = field(default_factory=dict)  # key -> name
    skipped: list[tuple[str, int]] = field(default_factory=list)  # key -> number of the task imported before
    problems: list[Problem] = field(default_factory=list)

    @property
    def refused(self) -> bool:
        return any(problem.level == ERROR for problem in self.problems)

    def links(self) -> int:
        return sum(len(item.targets) for item in self.tasks)


def _repeated(names: list[str]) -> list[str]:
    return sorted({name for name in names if names.count(name) > 1})


async def _people(conn: AsyncConnection, writer: writes.Writer, data: TasksFile, plan: Plan) -> dict[str, str | None]:
    """person key -> the login a task names them by, None for those it cannot."""
    logins = {person.key: (person.github or "").strip() or None for person in data.people}
    u, g = tables.users, tables.grants
    wanted = sorted({login for login in logins.values() if login})
    query = (
        select(u.c.login)
        .join_from(u, g, g.c.user_id == u.c.id)
        .where(g.c.project_id == writer.access.project_id, u.c.login.in_(wanted))
    )
    granted = set((await conn.execute(query)).scalars()) if wanted else set()
    for person in data.people:
        login = logins[person.key]
        if login is None:
            plan.problems.append(Problem(WARNING, f"person {person.key} has no GitHub login: their slots stay empty"))
        elif login not in granted:
            said = (
                f"person {person.key} ({login}) holds no grant on project {writer.access.name}: their slots stay empty"
            )
            plan.problems.append(Problem(WARNING, said))
            logins[person.key] = None
    return logins


async def _sprints(conn: AsyncConnection, writer: writes.Writer, data: TasksFile, plan: Plan) -> None:
    s = tables.tasks.sprints
    query = select(s.c.name, s.c.state).where(s.c.project_id == writer.access.project_id)
    held = {row.name: row.state for row in await conn.execute(query)}
    active = [name for name, state in held.items() if state == "active"]
    for sprint in data.sprints:
        plan.sprint_names[sprint.key] = sprint.name
        if sprint.name in held:
            plan.problems.append(
                Problem(WARNING, f"sprint {sprint.name!r} is in the project already: its tasks join it")
            )
            continue
        if sprint.state == "active":
            active.append(sprint.name)
        plan.sprints.append(
            writes.SprintInput(sprint.name, sprint.goal, sprint.starts_on, sprint.ends_on, sprint.state)
        )
    if len(active) > 1:
        plan.problems.append(Problem(ERROR, f"one sprint is active at a time; these would be: {', '.join(active)}"))


def _external(task: TasksFileTask) -> str | None:
    if task.external is None:
        return None
    return f"{task.external.system}:{task.external.id}"[:MAX_EXTERNAL_KEY_CHARS]


async def _imported_before(conn: AsyncConnection, project_id: int, keys: list[str]) -> dict[str, int]:
    t = tables.tasks.items
    query = select(t.c.external_key, t.c.number).where(t.c.project_id == project_id, t.c.external_key.in_(keys))
    return {row.external_key: row.number for row in await conn.execute(query)} if keys else {}


def _slot(people: dict[str, str | None], keys: list[str], task: str, plan: Plan) -> list[str]:
    found = []
    for key in keys:
        if key not in people:
            plan.problems.append(Problem(WARNING, f"no person {key} in the file's people", task))
        elif people[key]:
            found.append(people[key])
    return found


async def _targets(conn: AsyncConnection, writer: writes.Writer, task: TasksFileTask, plan: Plan) -> tuple[list, dict]:
    """The links of ``task`` the project has, and what it named that the project has not."""
    found, unmatched = [], {"use_cases": [], "plan_steps": [], "pulls": []}
    access, sink = writer.access, writer.sink
    attempts = [("use_cases", code, links.srs_item(conn, access, sink, code)) for code in task.use_cases]
    attempts += [
        ("plan_steps", f"{s.plan}#{s.step}", links.plan_step(conn, access, sink, s.plan, s.step))
        for s in task.plan_steps
    ]
    for what, named, attempt in attempts:
        try:
            found.append(await attempt)
        except TasksError as exc:
            unmatched[what].append(named)
            plan.problems.append(Problem(WARNING, f"{named} is not linked: {exc.message}", task.key))
    for url in task.pulls:
        try:
            found.append(links.pull(url))
        except TasksError as exc:
            unmatched["pulls"].append(url)
            plan.problems.append(Problem(WARNING, exc.message, task.key))
    return list(dict.fromkeys(found)), {name: values for name, values in unmatched.items() if values}


def _task_checks(writer: writes.Writer, data: TasksFile, task: TasksFileTask, plan: Plan) -> str | None:
    """The project's status of ``task``, after reporting what is wrong with it."""
    config = writer.settings
    given = task.status or config.statuses[0].key
    status = data.status_map.get(given, given)
    if config.status(status) is None:
        said = f"status {given!r} is no status of the project and status_map does not map it"
        plan.problems.append(Problem(ERROR, said, task.key))
    if task.type is not None and task.type not in config.types:
        said = f"type {task.type!r} is none of the project's: {', '.join(config.types)}"
        plan.problems.append(Problem(ERROR, said, task.key))
    if task.priority not in PRIORITIES:
        plan.problems.append(Problem(ERROR, f"priority {task.priority!r} is none of {', '.join(PRIORITIES)}", task.key))
    if task.sprint is not None and task.sprint not in plan.sprint_names:
        plan.problems.append(Problem(ERROR, f"no sprint {task.sprint} in the file's sprints", task.key))
    return status


async def plan(conn: AsyncConnection, writer: writes.Writer, data: TasksFile) -> Plan:
    """What importing ``data`` would do, and what is wrong with it; nothing written."""
    found = Plan()
    keys = [task.key for task in data.tasks]
    for what, names in (
        ("task", keys),
        ("person", [p.key for p in data.people]),
        ("sprint", [s.key for s in data.sprints]),
    ):
        if _repeated(names):
            found.problems.append(Problem(ERROR, f"{what} keys repeat: {', '.join(_repeated(names))}"))
    externals = [key for key in map(_external, data.tasks) if key]
    if _repeated(externals):
        found.problems.append(Problem(ERROR, f"external ids repeat: {', '.join(_repeated(externals))}"))
    people = await _people(conn, writer, data, found)
    await _sprints(conn, writer, data, found)
    before = await _imported_before(conn, writer.access.project_id, externals)
    for task in data.tasks:
        external = _external(task)
        if external in before:
            found.skipped.append((task.key, before[external]))
            ref = writer.settings.ref(before[external])
            found.problems.append(Problem(WARNING, f"{external} was imported before, as {ref}: skipped", task.key))
            continue
        status = _task_checks(writer, data, task, found)
        targets, unmatched = await _targets(conn, writer, task, found)
        ref = {**task.external.model_dump(exclude_none=True)} if task.external else {}
        ref |= {f"unmatched_{name}": values for name, values in unmatched.items()}
        roles = {slot: _slot(people, getattr(task.roles, slot), task.key, found) for slot in SLOTS}
        given = writes.TaskInput(
            title=task.title,
            description_md=task.description_md,
            type=task.type,
            status=status,
            priority=task.priority,
            due_on=task.due_on,
            sprint=found.sprint_names.get(task.sprint) if task.sprint else None,
            assignees=_slot(people, task.assignees, task.key, found),
            roles=roles,
            external_key=external,
            external_ref=ref or None,
        )
        found.tasks.append(Planned(task.key, given, targets, task.parent, task.done_on))
    planned = {item.key for item in found.tasks} | {key for key, _ in found.skipped}
    for item in found.tasks:
        if item.parent is not None and item.parent not in planned:
            found.problems.append(Problem(WARNING, f"no task {item.parent} in the file to be the parent", item.key))
    return found


async def apply(conn: AsyncConnection, writer: writes.Writer, found: Plan) -> list[int]:
    """Write what ``plan`` found; the numbers of the tasks made, in the file's order."""
    if found.refused:
        problems = [problem.as_json() for problem in found.problems]
        raise TasksError(422, "the file has errors: nothing was imported; see each with --dry-run", detail=problems)
    for sprint in found.sprints:
        await writes.create_sprint(conn, writer, sprint)
    numbers = {key: number for key, number in found.skipped}
    made = []
    for item in found.tasks:
        number = await writes.create(conn, writer, item.task, targets=item.targets)
        numbers[item.key] = number
        made.append(number)
        if item.done_on is not None:
            at = datetime.combine(item.done_on, time(), UTC)
            task_id = await _task_id(conn, writer.access.project_id, number)
            cause = f"done on {item.done_on.isoformat()}, as the import says"
            await writes.record(conn, task_id, writer.user_id, "status", None, item.task.status, cause=cause, at=at)
    t = tables.tasks.items
    for item in found.tasks:
        if item.parent in numbers:
            parent_id = await _task_id(conn, writer.access.project_id, numbers[item.parent])
            child = (t.c.project_id == writer.access.project_id) & (t.c.number == numbers[item.key])
            await conn.execute(update(t).where(child).values(parent_id=parent_id))
    return made


async def _task_id(conn: AsyncConnection, project_id: int, number: int) -> int:
    t = tables.tasks.items
    return await conn.scalar(select(t.c.id).where(t.c.project_id == project_id, t.c.number == number))


__all__ = [
    "FORMAT",
    "MAX_TASKS",
    "Plan",
    "Problem",
    "TasksFile",
    "TasksFileExternal",
    "TasksFilePerson",
    "TasksFileRoles",
    "TasksFileSprint",
    "TasksFileStep",
    "TasksFileTask",
    "apply",
    "plan",
]
