"""Keeping tasks: a new task (``create``), a change of one (``edit``), its people, its links, sprints and comments.
Every change of a task is a row of its history (``record``), with the member who made it, or the hub (no actor) and
why. A new task and a change of its status emit ``tasks.status`` too (``announce``, ``evo_agents.hub.task_status``).

A task's status is a key of the config's statuses and its type one of its types; its people hold a grant on the
project; its sprint is one of the project's that is not closed; its parent is another task of the project, never one
below it. A target of a link is one the member sees (``links``). The caller holds the transaction and has checked
the writer role; a refusal raises TasksError, and the transaction writes nothing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.tasks import PRIORITIES, links
from evo_agents.core_plugins.tasks.settings import Settings, TasksError, number_of
from evo_agents.core_plugins.tasks.store import SLOTS, People, TaskRow, members, people, sprint, visible
from evo_agents.hub import tables, task_status
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.tables_tasks import MAX_VALUE_CHARS

MAX_DEPTH = 50  # of a chain of parents


@dataclass(frozen=True)
class Writer:
    """Who writes, where, and through which sink they read what they link to."""

    access: ProjectAccess
    user_id: int
    settings: Settings
    sink: str | None = None


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


def _text(value) -> str | None:
    if value is None:
        return None
    return str(value)[:MAX_VALUE_CHARS]


async def announce(
    conn: AsyncConnection, writer: Writer, task_id: int, old: str | None, new: str, cause: str = task_status.CREATE
) -> None:
    """Emit ``tasks.status`` of task ``task_id``, whose status ``writer``'s member set (``old`` None) or changed."""
    settings = writer.settings
    change = task_status.StatusChange(
        task_id, old, new, settings.group(old), settings.group(new), cause, settings.prefix, writer.user_id
    )
    await task_status.emit(conn, writer.access.project_id, change)


async def record(
    conn: AsyncConnection,
    task_id: int,
    actor_id: int | None,
    name: str,
    old=None,
    new=None,
    *,
    cause: str = "",
    at: datetime | None = None,
) -> None:
    """A row of the history of task ``task_id``: ``actor_id`` (None: the hub) changed ``name`` from ``old`` to ``new``."""
    values = {
        "task_id": task_id,
        "actor_id": actor_id,
        "field": name,
        "from_value": _text(old),
        "to_value": _text(new),
        "cause": cause,
    }
    if at is not None:
        values["at"] = at
    await conn.execute(insert(tables.tasks.events).values(**values))


# Checks


def _status(settings: Settings, key: str | None) -> str:
    chosen = key or settings.statuses[0].key
    if settings.status(chosen) is None:
        known = ", ".join(status.key for status in settings.statuses)
        raise TasksError(422, f"no status {chosen} in this project; its statuses are {known}")
    return chosen


def _type(settings: Settings, name: str | None) -> str:
    chosen = name or settings.types[0]
    if chosen not in settings.types:
        raise TasksError(422, f"no task type {chosen} in this project; its types are {', '.join(settings.types)}")
    return chosen


def _priority(name: str) -> str:
    if name not in PRIORITIES:
        raise TasksError(422, f"no priority {name}; a task is {', '.join(PRIORITIES)}")
    return name


async def _sprint_id(conn: AsyncConnection, writer: Writer, name: str | None) -> int | None:
    if name is None:
        return None
    found = await sprint(conn, writer.access.project_id, name)
    if found is None:
        raise TasksError(422, f"no sprint {name!r} in project {writer.access.name}")
    if found.state == "closed":
        raise TasksError(422, f"sprint {name!r} is closed: put the task in an open one, or in none")
    return found.id


async def _parent_id(conn: AsyncConnection, writer: Writer, ref: str | None, task_id: int | None) -> int | None:
    """The id of the task ``ref`` names, which the member sees, when it is not ``task_id`` nor below it."""
    if ref is None:
        return None
    t = tables.tasks.items
    query = select(t.c.id, t.c.parent_id, t.c.label).where(
        t.c.project_id == writer.access.project_id, t.c.number == number_of(ref)
    )
    row = (await conn.execute(query)).first()
    if row is None or not visible(writer.access, row.label, writer.sink):
        raise TasksError(422, f"no task {ref} in project {writer.access.name} that you can see, to be the parent")
    above, depth = row, 0
    while above is not None and depth < MAX_DEPTH:
        if above.id == task_id:
            raise TasksError(422, f"task {ref} is this task or below it: a task is never its own parent")
        if above.parent_id is None:
            break
        above = (await conn.execute(select(t.c.id, t.c.parent_id).where(t.c.id == above.parent_id))).first()
        depth += 1
    return row.id


def _slots(assignees: list[str] | None, roles: dict[str, list[str]]) -> dict[str, list[str]]:
    """role -> its logins, for each slot given, without repeats in the order given."""
    given = {} if assignees is None else {"assignee": assignees}
    for role, logins in roles.items():
        if role not in SLOTS:
            raise TasksError(422, f"no role {role} on a task; its roles are {', '.join(SLOTS)}")
        given[role] = logins
    return {
        role: list(dict.fromkeys(login.strip() for login in logins if login.strip())) for role, logins in given.items()
    }


async def _targets(conn: AsyncConnection, writer: Writer, given: LinksIn) -> list[links.Target]:
    access, sink = writer.access, writer.sink
    found = [await links.srs_item(conn, access, sink, code) for code in given.use_cases]
    for step in given.steps:
        found.append(await links.plan_step(conn, access, sink, *links.split_step(step)))
    found += [await links.run(conn, access, run_id) for run_id in given.runs]
    found += [links.pull(url) for url in given.pulls]
    return list(dict.fromkeys(found))


# Writing


async def _set_people(
    conn: AsyncConnection, writer: Writer, task_id: int, slots: dict[str, list[str]], held: People
) -> None:
    ids = await members(conn, writer.access, {login for logins in slots.values() for login in logins})
    p = tables.tasks.people
    for role, logins in slots.items():
        before = held.assignees if role == "assignee" else held.roles[role]
        if sorted(before) == sorted(logins):
            continue
        await conn.execute(delete(p).where(p.c.task_id == task_id, p.c.role == role))
        if logins:
            await conn.execute(insert(p), [{"task_id": task_id, "role": role, "user_id": ids[n]} for n in logins])
        await record(conn, task_id, writer.user_id, role, ", ".join(before) or None, ", ".join(logins) or None)


async def _add_links(conn: AsyncConnection, writer: Writer, task_id: int, targets: list[links.Target]) -> None:
    k = tables.tasks.links
    held = {
        links.Target(row.kind, row.srs_item_id, row.plan_id, row.step_id, row.run_id, row.url)
        for row in await conn.execute(select(k).where(k.c.task_id == task_id))
    }
    for target in targets:
        if target in held:
            continue
        values = {"task_id": task_id, "created_by": writer.user_id, **target.columns()}
        await conn.execute(insert(k).values(**values))
        await record(conn, task_id, writer.user_id, "link", None, f"{target.kind} {target.name}")


async def _unlink(conn: AsyncConnection, writer: Writer, task_id: int, link_ids: list[int]) -> None:
    k = tables.tasks.links
    for link_id in link_ids:
        gone = await conn.execute(delete(k).where(k.c.id == link_id, k.c.task_id == task_id).returning(k.c.kind))
        kind = gone.scalar()
        if kind is None:
            raise TasksError(422, f"the task has no link {link_id}: see its links with `evo-agents hub task show`")
        await record(conn, task_id, writer.user_id, "link", f"{kind} link {link_id}", None)


async def _next_number(conn: AsyncConnection, project_id: int) -> int:
    t = tables.tasks.items
    last = await conn.scalar(select(func.max(t.c.number)).where(t.c.project_id == project_id))
    return (last or 0) + 1


async def create(
    conn: AsyncConnection,
    writer: Writer,
    task: TaskInput,
    *,
    targets: list[links.Target] | None = None,
    cause: str | None = task_status.CREATE,
) -> int:
    """Keep a new task, linked to ``targets`` when given (found already), else to what ``task.links`` names, and emit
    its status with ``cause`` (None: the caller emits it once it has set what is left, as an import sets parents); its
    number."""
    settings, access = writer.settings, writer.access
    values = {
        "project_id": access.project_id,
        "number": await _next_number(conn, access.project_id),
        "type": _type(settings, task.type),
        "title": task.title,
        "description_md": task.description_md,
        "status": _status(settings, task.status),
        "priority": _priority(task.priority),
        "due_on": task.due_on,
        "sprint_id": await _sprint_id(conn, writer, task.sprint),
        "parent_id": await _parent_id(conn, writer, task.parent, None),
        "label": access.push_label(task.label),
        "external_key": task.external_key,
        "external_ref": task.external_ref,
        "created_by": writer.user_id,
    }
    targets = await _targets(conn, writer, task.links) if targets is None else targets
    t = tables.tasks.items
    task_id = await conn.scalar(insert(t).values(**values).returning(t.c.id))
    await record(conn, task_id, writer.user_id, "created", None, settings.ref(values["number"]))
    await _set_people(conn, writer, task_id, _slots(task.assignees, task.roles), People())
    await _add_links(conn, writer, task_id, targets)
    if cause is not None:
        await announce(conn, writer, task_id, None, values["status"], cause)
    return values["number"]


FIELDS = ("title", "description_md", "type", "status", "priority", "due_on", "sprint", "parent")


async def _relation(conn: AsyncConnection, writer: Writer, row: TaskRow, name: str, value: str | None):
    """(old, new, column value) of the sprint or the parent of task ``row`` set to ``value``, old and new as its
    history shows them; None when it stays."""
    if name == "sprint":
        return None if row.sprint == value else (row.sprint, value, await _sprint_id(conn, writer, value))
    number = number_of(value) if value is not None else None
    if row.parent_number == number:
        return None
    ref = writer.settings.ref
    old = ref(row.parent_number) if row.parent_number else None
    return old, ref(number) if number else None, await _parent_id(conn, writer, value, row.id)


async def _changed_values(conn: AsyncConnection, writer: Writer, row: TaskRow, values: dict) -> dict:
    """The columns ``values`` changes on task ``row``, each checked; column -> (old, new, column value)."""
    unknown = sorted(set(values) - set(FIELDS))
    if unknown:
        raise TasksError(422, f"a task has no field {', '.join(unknown)}")
    checks = {
        "type": lambda v: _type(writer.settings, v),
        "status": lambda v: _status(writer.settings, v),
        "priority": _priority,
    }
    found = {}
    for name, value in values.items():
        if name in ("sprint", "parent"):
            change = await _relation(conn, writer, row, name, value)
            if change is not None:
                found[f"{name}_id"] = change
            continue
        checked = checks[name](value) if name in checks else value
        if getattr(row, name) != checked:
            found[name] = (getattr(row, name), checked, checked)
    return found


async def edit(conn: AsyncConnection, writer: Writer, row: TaskRow, changes: TaskChanges) -> None:
    """Apply ``changes`` to task ``row``, whose row lock the caller holds."""
    found = await _changed_values(conn, writer, row, changes.values)
    targets = await _targets(conn, writer, changes.links)
    t = tables.tasks.items
    if found:
        columns = {name: column for name, (_, _, column) in found.items()}
        await conn.execute(update(t).where(t.c.id == row.id).values(**columns, updated_at=func.now()))
        for name, (old, new, _) in found.items():
            shown = name.removesuffix("_id")
            if shown == "description_md":
                await record(conn, row.id, writer.user_id, "description")
            else:
                await record(conn, row.id, writer.user_id, shown, old, new)
    slots = _slots(changes.assignees, changes.roles)
    if slots or targets or changes.unlink:
        held = (await people(conn, [row.id]))[row.id]
        await _set_people(conn, writer, row.id, slots, held)
        await _unlink(conn, writer, row.id, changes.unlink)
        await _add_links(conn, writer, row.id, targets)
        await conn.execute(update(t).where(t.c.id == row.id).values(updated_at=func.now()))
    if "status" in found:  # with the links and parent the edit leaves
        old, new, _ = found["status"]
        await announce(conn, writer, row.id, old, new, task_status.EDIT)


async def comment(conn: AsyncConnection, writer: Writer, task_id: int, body: str) -> int:
    c = tables.tasks.comments
    values = {"task_id": task_id, "author_id": writer.user_id, "body": body}
    return await conn.scalar(insert(c).values(**values).returning(c.c.id))


# Sprints


@dataclass
class SprintInput:
    name: str
    goal: str = ""
    starts_on: date | None = None
    ends_on: date | None = None
    state: str = "planned"


async def _no_other_active(conn: AsyncConnection, project_id: int, sprint_id: int | None) -> None:
    s = tables.tasks.sprints
    query = select(s.c.name).where(s.c.project_id == project_id, s.c.state == "active")
    if sprint_id is not None:
        query = query.where(s.c.id != sprint_id)
    active = await conn.scalar(query)
    if active is not None:
        raise TasksError(409, f"sprint {active!r} is active: close it first, a project runs one sprint at a time")


def _dates(starts_on: date | None, ends_on: date | None) -> None:
    if starts_on and ends_on and ends_on < starts_on:
        raise TasksError(422, f"a sprint ends on or after it starts; {ends_on} is before {starts_on}")


async def create_sprint(conn: AsyncConnection, writer: Writer, given: SprintInput) -> None:
    project_id = writer.access.project_id
    if await sprint(conn, project_id, given.name) is not None:
        raise TasksError(409, f"project {writer.access.name} has a sprint {given.name!r} already")
    if given.state == "closed":
        raise TasksError(422, "a new sprint is planned or active, not closed")
    if given.state == "active":
        await _no_other_active(conn, project_id, None)
    _dates(given.starts_on, given.ends_on)
    values = {"project_id": project_id, "created_by": writer.user_id, **asdict(given)}
    await conn.execute(insert(tables.tasks.sprints).values(**values))


async def edit_sprint(conn: AsyncConnection, writer: Writer, name: str, values: dict, move_to: str | None) -> str:
    """Change sprint ``name``; closing it moves its unfinished tasks to sprint ``move_to``, or to no sprint. Its name
    as it is now."""
    project_id = writer.access.project_id
    held = await sprint(conn, project_id, name, lock=True)
    if held is None:
        raise TasksError(404, f"no sprint {name!r} in project {writer.access.name}")
    if held.state == "closed":
        raise TasksError(409, f"sprint {name!r} is closed")
    _dates(values.get("starts_on", held.starts_on), values.get("ends_on", held.ends_on))
    state = values.get("state", held.state)
    if state == "active" and held.state != "active":
        await _no_other_active(conn, project_id, held.id)
    new_name = values.get("name", held.name)
    if new_name != held.name and await sprint(conn, project_id, new_name) is not None:
        raise TasksError(409, f"project {writer.access.name} has a sprint {new_name!r} already")
    s, t = tables.tasks.sprints, tables.tasks.items
    if state == "closed":
        target = await _sprint_id(conn, writer, move_to) if move_to else None
        if target == held.id:
            raise TasksError(422, "a closing sprint moves its unfinished tasks to another sprint, or to none")
        open_keys = [status.key for status in writer.settings.statuses if not writer.settings.finished(status.key)]
        moving = (t.c.sprint_id == held.id) & t.c.status.in_(open_keys)
        moved = (await conn.execute(update(t).where(moving).values(sprint_id=target).returning(t.c.id))).scalars()
        for task_id in moved.all():
            await record(
                conn, task_id, writer.user_id, "sprint", held.name, move_to, cause=f"sprint {held.name} closed"
            )
        values = {**values, "closed_at": func.now()}
    await conn.execute(update(s).where(s.c.id == held.id).values(**values))
    return new_name


__all__ = [
    "FIELDS",
    "LinksIn",
    "SprintInput",
    "TaskChanges",
    "TaskInput",
    "Writer",
    "announce",
    "comment",
    "create",
    "create_sprint",
    "edit",
    "edit_sprint",
    "record",
]
