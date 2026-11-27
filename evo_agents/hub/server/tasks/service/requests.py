"""What each route of the plugin tasks does in its transaction (``routes``): the member's access opened and checked
first, then the read or the write, then the answer as its model. A write that changes the tasks queues a build of
the project's knowledge graph on ``jobs``, whose tickets they are.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server.security import holds
from evo_agents.hub.server.tasks.models.api import (
    TasksBlocker,
    TasksBlockerEdit,
    TasksBlockerIn,
    TasksComment,
    TasksCommentIn,
    TasksImport,
    TasksImportResult,
    TasksItem,
    TasksItemDetail,
    TasksItemEdit,
    TasksItemIn,
    TasksPerson,
    TasksSettings,
    TasksSprint,
    TasksSprintEdit,
    TasksSprintIn,
    blocker_model,
    comment_model,
    import_model,
    settings_model,
    sprint_model,
)
from evo_agents.hub.server.tasks.models.values import (
    Filters,
    LinksIn,
    SprintInput,
    TaskChanges,
    TaskInput,
    TasksError,
    number_of,
)
from evo_agents.hub.server.tasks.repository import history
from evo_agents.hub.server.tasks.service import blockers, imports, reads, writes
from evo_agents.hub.server.tasks.service.project import graph_changed, opened, srs_enabled, writer_of

# Reading


async def show_settings(conn: AsyncConnection, user, project: str) -> TasksSettings:
    access, config = await opened(conn, user, project)
    return settings_model(config, await srs_enabled(conn, access.project_id))


async def list_tasks(conn: AsyncConnection, user, project: str, filters: Filters, sink) -> list[TasksItem]:
    access, config = await opened(conn, user, project)
    rows = await reads.tasks(conn, access, sink, filters)
    return await reads.items(conn, access, config, rows, sink)


async def show_task(conn: AsyncConnection, user, project: str, ref: str, sink) -> TasksItemDetail:
    access, config = await opened(conn, user, project)
    return await reads.task_detail(conn, access, config, number_of(ref), sink)


async def list_sprints(conn: AsyncConnection, user, project: str) -> list[TasksSprint]:
    access, _ = await opened(conn, user, project)
    return [sprint_model(row) for row in await reads.all_sprints(conn, access)]


async def list_people(conn: AsyncConnection, user, project: str) -> list[TasksPerson]:
    access, _ = await opened(conn, user, project)
    return [TasksPerson(login=row.login, role=row.role) for row in await reads.project_members(conn, access)]


# Writing


async def create_task(conn: AsyncConnection, jobs, user, project: str, body: TasksItemIn, sink) -> TasksItemDetail:
    access, config = await opened(conn, user, project)
    writer = writer_of(access, user, sink, config, "creating a task")
    if body.label is not None and not holds(access.role, "admin"):
        raise TasksError(403, f"giving a task a label of its own in project {access.name} takes an admin")
    given = body.model_dump(exclude={"roles", "links", "label"})
    task = TaskInput(
        **given,
        roles=body.roles.given(),
        links=LinksIn(**body.links.model_dump()),
        label=body.label.model_dump(exclude_none=True) if body.label else None,
    )
    number = await writes.create(conn, writer, task)
    await graph_changed(jobs, conn, access)
    return await reads.task_detail(conn, access, config, number, sink)


async def edit_task(
    conn: AsyncConnection, jobs, user, project: str, ref: str, body: TasksItemEdit, sink
) -> TasksItemDetail:
    access, config = await opened(conn, user, project)
    writer = writer_of(access, user, sink, config, "changing a task")
    row = await reads.task(conn, access, number_of(ref), sink, lock=True)
    changes = TaskChanges(
        values=body.values(),
        assignees=body.assignees,
        roles=body.roles.given(),
        links=LinksIn(**body.links.model_dump()),
        unlink=body.unlink,
    )
    await writes.edit(conn, writer, row, changes)
    await graph_changed(jobs, conn, access)
    return await reads.task_detail(conn, access, config, row.number, sink)


async def add_comment(conn: AsyncConnection, user, project: str, ref: str, body: TasksCommentIn, sink) -> TasksComment:
    access, config = await opened(conn, user, project)
    writer = writer_of(access, user, sink, config, "commenting on a task")
    row = await reads.task(conn, access, number_of(ref), sink)
    comment_id = await writes.comment(conn, writer, row.id, body.body)
    return comment_model(next(c for c in await history.comments(conn, row.id) if c.id == comment_id))


async def add_blocker(conn: AsyncConnection, user, project: str, ref: str, body: TasksBlockerIn, sink) -> TasksBlocker:
    access, config = await opened(conn, user, project)
    writer = writer_of(access, user, sink, config, "blocking a task")
    row = await reads.task(conn, access, number_of(ref), sink, lock=True)
    return blocker_model(await blockers.put(conn, writer, row, body.reason, body.waiting()))


async def edit_blocker(
    conn: AsyncConnection, user, project: str, ref: str, blocker_id: int, body: TasksBlockerEdit, sink
) -> TasksBlocker:
    access, config = await opened(conn, user, project)
    writer = writer_of(access, user, sink, config, "changing a blocker")
    row = await reads.task(conn, access, number_of(ref), sink, lock=True)
    changed = await blockers.change(
        conn, writer, row, blocker_id, reason=body.reason, waiting=body.waiting(), clear=body.cleared
    )
    return blocker_model(changed)


async def create_sprint(conn: AsyncConnection, user, project: str, body: TasksSprintIn) -> TasksSprint:
    access, config = await opened(conn, user, project)
    writer = writer_of(access, user, None, config, "creating a sprint")
    await writes.create_sprint(conn, writer, SprintInput(**body.model_dump()))
    return sprint_model(await reads.sprint(conn, access, body.name))


async def edit_sprint(conn: AsyncConnection, user, project: str, name: str, body: TasksSprintEdit) -> TasksSprint:
    access, config = await opened(conn, user, project)
    writer = writer_of(access, user, None, config, "changing a sprint")
    now = await writes.edit_sprint(conn, writer, name, body.values(), body.move_to)
    return sprint_model(await reads.sprint(conn, access, now))


async def import_tasks(conn: AsyncConnection, jobs, user, project: str, body: TasksImport, sink) -> TasksImportResult:
    access, config = await opened(conn, user, project)
    writer = writer_of(access, user, sink, config, "importing tasks")
    found = await imports.plan(conn, writer, body.data)
    made = [] if body.dry_run else await imports.apply(conn, writer, found)
    if made:
        await graph_changed(jobs, conn, access)
    return import_model(config, found, body.dry_run, made)


__all__ = [
    "add_blocker",
    "add_comment",
    "create_sprint",
    "create_task",
    "edit_blocker",
    "edit_sprint",
    "edit_task",
    "import_tasks",
    "list_people",
    "list_sprints",
    "list_tasks",
    "show_settings",
    "show_task",
]
