"""Importing tasks once from ``evo-tasks/1`` JSON (``models.files.TasksFile``): its people, its sources' statuses
mapped to the project's, its sprints and its tasks with their slots and links, use cases named by code.

``plan`` reads the file against the project and writes nothing: each task's status (through ``status_map``, else as
given) must be a status of the project and its type one of its types, its people members with a grant, its sprint
one of the file's or the project's, its links targets the member sees. What stops an import is an error; what it
leaves out is a warning: a person with no GitHub login or no grant, a use case code no SRS item holds (kept in the
task's ``external_ref``), a plan step that is not there, a parent the file does not have. A task whose source
(``external``: system and id) an import brought in already is skipped. ``apply`` writes what ``plan`` found, every
task numbered in the file's order, its done date a row of its history, and each emits ``tasks.status`` (cause
``import``) once the parents are set.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import task_status
from evo_agents.hub.server.tasks import PRIORITIES
from evo_agents.hub.server.tasks.models.files import ERROR, WARNING, Plan, Planned, Problem, TasksFile, TasksFileTask
from evo_agents.hub.server.tasks.models.values import SLOTS, SprintInput, TaskInput, TasksError, Writer
from evo_agents.hub.server.tasks.repository import history, sprints
from evo_agents.hub.server.tasks.repository import tasks as task_rows
from evo_agents.hub.server.tasks.service import links, writes
from evo_agents.hub.tables_tasks import MAX_EXTERNAL_KEY_CHARS


def _repeated(names: list[str]) -> list[str]:
    return sorted({name for name in names if names.count(name) > 1})


async def _people(conn: AsyncConnection, writer: Writer, data: TasksFile, plan: Plan) -> dict[str, str | None]:
    """person key -> the login a task names them by, None for those it cannot."""
    logins = {person.key: (person.github or "").strip() or None for person in data.people}
    wanted = {login for login in logins.values() if login}
    granted = set(await task_rows.members(conn, writer.access.project_id, wanted)) if wanted else set()
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


async def _sprints(conn: AsyncConnection, writer: Writer, data: TasksFile, plan: Plan) -> None:
    held = await sprints.states(conn, writer.access.project_id)
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
        plan.sprints.append(SprintInput(sprint.name, sprint.goal, sprint.starts_on, sprint.ends_on, sprint.state))
    if len(active) > 1:
        plan.problems.append(Problem(ERROR, f"one sprint is active at a time; these would be: {', '.join(active)}"))


def _external(task: TasksFileTask) -> str | None:
    if task.external is None:
        return None
    return f"{task.external.system}:{task.external.id}"[:MAX_EXTERNAL_KEY_CHARS]


def _slot(people: dict[str, str | None], keys: list[str], task: str, plan: Plan) -> list[str]:
    found = []
    for key in keys:
        login = people.get(key)
        if key not in people:
            plan.problems.append(Problem(WARNING, f"no person {key} in the file's people", task))
        elif login:
            found.append(login)
    return found


async def _targets(conn: AsyncConnection, writer: Writer, task: TasksFileTask, plan: Plan) -> tuple[list, dict]:
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


def _task_checks(writer: Writer, data: TasksFile, task: TasksFileTask, plan: Plan) -> str:
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


async def _planned(
    conn: AsyncConnection, writer: Writer, data: TasksFile, task: TasksFileTask, found: Plan, people: dict
) -> Planned:
    status = _task_checks(writer, data, task, found)
    targets, unmatched = await _targets(conn, writer, task, found)
    ref = {**task.external.model_dump(exclude_none=True)} if task.external else {}
    ref |= {f"unmatched_{name}": values for name, values in unmatched.items()}
    roles = {slot: _slot(people, getattr(task.roles, slot), task.key, found) for slot in SLOTS}
    given = TaskInput(
        title=task.title,
        description_md=task.description_md,
        type=task.type,
        status=status,
        priority=task.priority,
        due_on=task.due_on,
        sprint=found.sprint_names.get(task.sprint) if task.sprint else None,
        assignees=_slot(people, task.assignees, task.key, found),
        roles=roles,
        external_key=_external(task),
        external_ref=ref or None,
    )
    return Planned(task.key, given, targets, task.parent, task.done_on)


async def plan(conn: AsyncConnection, writer: Writer, data: TasksFile) -> Plan:
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
    before = await task_rows.imported_before(conn, writer.access.project_id, externals)
    for task in data.tasks:
        external = _external(task)
        if external is not None and external in before:
            found.skipped.append((task.key, before[external]))
            ref = writer.settings.ref(before[external])
            found.problems.append(Problem(WARNING, f"{external} was imported before, as {ref}: skipped", task.key))
            continue
        found.tasks.append(await _planned(conn, writer, data, task, found, people))
    planned = {item.key for item in found.tasks} | {key for key, _ in found.skipped}
    for item in found.tasks:
        if item.parent is not None and item.parent not in planned:
            found.problems.append(Problem(WARNING, f"no task {item.parent} in the file to be the parent", item.key))
    return found


async def apply(conn: AsyncConnection, writer: Writer, found: Plan) -> list[int]:
    """Write what ``plan`` found; the numbers of the tasks made, in the file's order."""
    if found.refused:
        problems = [problem.as_json() for problem in found.problems]
        raise TasksError(422, "the file has errors: nothing was imported; see each with --dry-run", detail=problems)
    for sprint in found.sprints:
        await writes.create_sprint(conn, writer, sprint)
    numbers = {key: number for key, number in found.skipped}
    made = []
    project_id = writer.access.project_id
    for item in found.tasks:
        number = await writes.create(conn, writer, item.task, targets=item.targets, cause=None)
        numbers[item.key] = number
        made.append(number)
        if item.done_on is not None:
            at = datetime.combine(item.done_on, time(), UTC)
            task_id = await task_rows.task_id(conn, project_id, number)
            cause = f"done on {item.done_on.isoformat()}, as the import says"
            await history.record(conn, task_id, writer.user_id, "status", None, item.task.status, cause=cause, at=at)
    for item in found.tasks:
        if item.parent in numbers:
            parent_id = await task_rows.task_id(conn, project_id, numbers[item.parent])
            await task_rows.set_parent(conn, project_id, numbers[item.key], parent_id)
    for item in found.tasks:  # with its parent set
        task_id = await task_rows.task_id(conn, project_id, numbers[item.key])
        status = item.task.status or writer.settings.statuses[0].key  # as ``writes.create`` chose it
        await writes.announce(conn, writer, task_id, None, status, task_status.IMPORT)
    return made


__all__ = ["apply", "plan"]
