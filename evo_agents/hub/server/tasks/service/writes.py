"""Keeping tasks: a new task (``create``), a change of one (``edit``), its people, its links, sprints and comments.
Every change of a task is a row of its history (``repository.history.record``), with the member who made it, or the
hub (no actor) and why. A new task and a change of its status emit ``tasks.status`` too (``announce``,
``evo_agents.hub.task_status``).

A task's status is a key of the config's statuses and its type one of its types; its people hold a grant on the
project; its sprint is one of the project's that is not closed; its parent is another task of the project, never one
below it. A target of a link is one the member sees (``links``). The caller holds the transaction and has checked
the writer role; a refusal raises TasksError, and the transaction writes nothing.
"""

from __future__ import annotations

from dataclasses import asdict

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import task_status
from evo_agents.hub.server.tasks import PRIORITIES
from evo_agents.hub.server.tasks.models.rows import People, Target, TaskRow
from evo_agents.hub.server.tasks.models.values import (
    MAX_DEPTH,
    SLOTS,
    LinksIn,
    Settings,
    SprintInput,
    TaskChanges,
    TaskInput,
    TasksError,
    Writer,
    number_of,
    visible,
)
from evo_agents.hub.server.tasks.repository import history, sprints
from evo_agents.hub.server.tasks.repository import links as link_rows
from evo_agents.hub.server.tasks.repository import tasks as task_rows
from evo_agents.hub.server.tasks.service import links
from evo_agents.hub.server.tasks.service.project import members


async def announce(
    conn: AsyncConnection, writer: Writer, task_id: int, old: str | None, new: str, cause: str = task_status.CREATE
) -> None:
    """Emit ``tasks.status`` of task ``task_id``, whose status ``writer``'s member set (``old`` None) or changed."""
    settings = writer.settings
    change = task_status.StatusChange(
        task_id, old, new, settings.group(old), settings.group(new), cause, settings.prefix, writer.user_id
    )
    await task_status.emit(conn, writer.access.project_id, change)


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
    found = await sprints.sprint(conn, writer.access.project_id, name)
    if found is None:
        raise TasksError(422, f"no sprint {name!r} in project {writer.access.name}")
    if found.state == "closed":
        raise TasksError(422, f"sprint {name!r} is closed: put the task in an open one, or in none")
    return found.id


async def _parent_id(conn: AsyncConnection, writer: Writer, ref: str | None, task_id: int | None) -> int | None:
    """The id of the task ``ref`` names, which the member sees, when it is not ``task_id`` nor below it."""
    if ref is None:
        return None
    row = await task_rows.task_label(conn, writer.access.project_id, number_of(ref))
    if row is None or not visible(writer.access, row.label, writer.sink):
        raise TasksError(422, f"no task {ref} in project {writer.access.name} that you can see, to be the parent")
    above, depth = row, 0
    while above is not None and depth < MAX_DEPTH:
        if above.id == task_id:
            raise TasksError(422, f"task {ref} is this task or below it: a task is never its own parent")
        if above.parent_id is None:
            break
        above = await task_rows.parent_of(conn, above.parent_id)
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


async def _targets(conn: AsyncConnection, writer: Writer, given: LinksIn) -> list[Target]:
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
    for role, logins in slots.items():
        before = held.assignees if role == "assignee" else held.roles[role]
        if sorted(before) == sorted(logins):
            continue
        await task_rows.replace_people(conn, task_id, role, [ids[n] for n in logins])
        await history.record(conn, task_id, writer.user_id, role, ", ".join(before) or None, ", ".join(logins) or None)


async def _add_links(conn: AsyncConnection, writer: Writer, task_id: int, targets: list[Target]) -> None:
    held = await link_rows.held(conn, task_id)
    for target in targets:
        if target in held:
            continue
        await link_rows.insert_link(conn, task_id, writer.user_id, target)
        await history.record(conn, task_id, writer.user_id, "link", None, f"{target.kind} {target.name}")


async def _unlink(conn: AsyncConnection, writer: Writer, task_id: int, link_ids: list[int]) -> None:
    for link_id in link_ids:
        kind = await link_rows.delete_link(conn, task_id, link_id)
        if kind is None:
            raise TasksError(422, f"the task has no link {link_id}: see its links with `evo-agents hub task show`")
        await history.record(conn, task_id, writer.user_id, "link", f"{kind} link {link_id}", None)


async def create(
    conn: AsyncConnection,
    writer: Writer,
    task: TaskInput,
    *,
    targets: list[Target] | None = None,
    cause: str | None = task_status.CREATE,
) -> int:
    """Keep a new task, linked to ``targets`` when given (found already), else to what ``task.links`` names, and emit
    its status with ``cause`` (None: the caller emits it once it has set what is left, as an import sets parents); its
    number."""
    settings, access = writer.settings, writer.access
    values = {
        "project_id": access.project_id,
        "number": await task_rows.next_number(conn, access.project_id),
        "type": _type(settings, task.type),
        "title": task.title,
        "description_md": task.description_md,
        "outcome_md": task.outcome_md,
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
    task_id = await task_rows.insert_task(conn, values)
    await history.record(conn, task_id, writer.user_id, "created", None, settings.ref(values["number"]))
    await _set_people(conn, writer, task_id, _slots(task.assignees, task.roles), People())
    await _add_links(conn, writer, task_id, targets)
    if cause is not None:
        await announce(conn, writer, task_id, None, values["status"], cause)
    return values["number"]


FIELDS = ("title", "description_md", "outcome_md", "type", "status", "priority", "due_on", "sprint", "parent")


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
    if found:
        await task_rows.update_task(conn, row.id, {name: column for name, (_, _, column) in found.items()})
        for name, (old, new, _) in found.items():
            shown = name.removesuffix("_id")
            if shown in ("description_md", "outcome_md"):
                await history.record(conn, row.id, writer.user_id, shown.removesuffix("_md"))
            else:
                await history.record(conn, row.id, writer.user_id, shown, old, new)
    slots = _slots(changes.assignees, changes.roles)
    if slots or targets or changes.unlink:
        held = (await task_rows.people(conn, [row.id]))[row.id]
        await _set_people(conn, writer, row.id, slots, held)
        await _unlink(conn, writer, row.id, changes.unlink)
        await _add_links(conn, writer, row.id, targets)
        await task_rows.update_task(conn, row.id, {})
    if "status" in found:  # with the links and parent the edit leaves
        old, new, _ = found["status"]
        await announce(conn, writer, row.id, old, new, task_status.EDIT)


async def comment(conn: AsyncConnection, writer: Writer, task_id: int, body: str) -> int:
    return await history.add_comment(conn, task_id, writer.user_id, body)


# Sprints


async def _no_other_active(conn: AsyncConnection, project_id: int, sprint_id: int | None) -> None:
    active = await sprints.active_other(conn, project_id, sprint_id)
    if active is not None:
        raise TasksError(409, f"sprint {active!r} is active: close it first, a project runs one sprint at a time")


def _dates(starts_on, ends_on) -> None:
    if starts_on and ends_on and ends_on < starts_on:
        raise TasksError(422, f"a sprint ends on or after it starts; {ends_on} is before {starts_on}")


async def create_sprint(conn: AsyncConnection, writer: Writer, given: SprintInput) -> None:
    project_id = writer.access.project_id
    if await sprints.sprint(conn, project_id, given.name) is not None:
        raise TasksError(409, f"project {writer.access.name} has a sprint {given.name!r} already")
    if given.state == "closed":
        raise TasksError(422, "a new sprint is planned or active, not closed")
    if given.state == "active":
        await _no_other_active(conn, project_id, None)
    _dates(given.starts_on, given.ends_on)
    values = {"project_id": project_id, "created_by": writer.user_id, **asdict(given)}
    await sprints.insert_sprint(conn, values, activated=given.state == "active")


async def edit_sprint(conn: AsyncConnection, writer: Writer, name: str, values: dict, move_to: str | None) -> str:
    """Change sprint ``name``; closing it moves its unfinished tasks to sprint ``move_to``, or to no sprint. Its name
    as it is now."""
    project_id = writer.access.project_id
    held = await sprints.sprint(conn, project_id, name, lock=True)
    if held is None:
        raise TasksError(404, f"no sprint {name!r} in project {writer.access.name}")
    if held.state == "closed":
        raise TasksError(409, f"sprint {name!r} is closed")
    _dates(values.get("starts_on", held.starts_on), values.get("ends_on", held.ends_on))
    state = values.get("state", held.state)
    activated = state == "active" and held.state != "active"
    if activated:
        await _no_other_active(conn, project_id, held.id)
    new_name = values.get("name", held.name)
    if new_name != held.name and await sprints.sprint(conn, project_id, new_name) is not None:
        raise TasksError(409, f"project {writer.access.name} has a sprint {new_name!r} already")
    if state == "closed":
        target = await _sprint_id(conn, writer, move_to) if move_to else None
        if target == held.id:
            raise TasksError(422, "a closing sprint moves its unfinished tasks to another sprint, or to none")
        open_keys = [status.key for status in writer.settings.statuses if not writer.settings.finished(status.key)]
        for task_id in await sprints.move_tasks(conn, held.id, open_keys, target):
            await history.record(
                conn, task_id, writer.user_id, "sprint", held.name, move_to, cause=f"sprint {held.name} closed"
            )
    await sprints.update_sprint(conn, held.id, values, activated=activated, closed=state == "closed")
    return new_name


__all__ = ["FIELDS", "announce", "comment", "create", "create_sprint", "edit", "edit_sprint"]
