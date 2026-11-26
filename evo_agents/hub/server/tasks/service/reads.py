"""Reading the tasks of a project as a member sees them: the tasks a list keeps (``tasks``), one task (``task``), a
task whole (``task_detail``), the answer of a list (``items``), the sprints and the members a task may name.

A task is read under its label, through the sink X-Evo-Sink names, else the project's hub sink (``models.values.
visible``). A task the member cannot see answers the 404 of one that does not exist.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.tasks.models.api import TasksItem, TasksItemDetail, detail_model, item_model
from evo_agents.hub.server.tasks.models.rows import MemberRow, SprintRow, TaskRow
from evo_agents.hub.server.tasks.models.values import Filters, Settings, visible
from evo_agents.hub.server.tasks.repository import history, sprints
from evo_agents.hub.server.tasks.repository import tasks as repository
from evo_agents.hub.server.tasks.service import blockers, links
from evo_agents.hub.server.tasks.service.project import missing, reader


async def tasks(conn: AsyncConnection, access: ProjectAccess, sink: str | None, filters: Filters) -> list[TaskRow]:
    """The tasks of the project the member sees that ``filters`` keeps, by number, at most ``filters.limit``."""
    reader(access)
    rows = await repository.tasks(conn, access.project_id, filters)
    return [row for row in rows if visible(access, row.label, sink)][: filters.limit]


async def task(
    conn: AsyncConnection, access: ProjectAccess, number: int, sink: str | None, *, lock: bool = False
) -> TaskRow:
    """Task ``number`` as the member sees it; 404 when there is none or they cannot see it. ``lock`` takes its row
    lock first, for a write."""
    reader(access)
    row = await repository.task(conn, access.project_id, number, lock=lock)
    if row is None or not visible(access, row.label, sink):
        raise missing(access, str(number))
    return row


async def items(
    conn: AsyncConnection,
    access: ProjectAccess,
    config: Settings,
    rows: list[TaskRow],
    sink: str | None,
    waiting: dict | None = None,
) -> list[TasksItem]:
    """The tasks ``rows`` as a list answers them: each with its people, its progress, its codes and whether it is
    blocked."""
    ids = [row.id for row in rows]
    held = await repository.people(conn, ids)
    done = await links.progress(conn, access, sink, ids)
    coded = await links.codes(conn, access, sink, ids)
    waiting = await blockers.open_for(conn, config, ids) if waiting is None else waiting
    return [item_model(config, row, held[row.id], done[row.id], coded[row.id], bool(waiting[row.id])) for row in rows]


async def task_detail(
    conn: AsyncConnection, access: ProjectAccess, config: Settings, number: int, sink: str | None
) -> TasksItemDetail:
    """Task ``number`` whole, as ``access`` reads it through ``sink``: its links, blockers, history and comments."""
    row = await task(conn, access, number, sink)
    waiting = await blockers.open_for(conn, config, [row.id])
    [item] = await items(conn, access, config, [row], sink, waiting)
    shown = await links.shown(conn, access, sink, row.id)
    events, said = await history.events(conn, row.id), await history.comments(conn, row.id)
    return detail_model(item, row, shown, waiting[row.id], events, said)


async def all_sprints(conn: AsyncConnection, access: ProjectAccess) -> list[SprintRow]:
    """The sprints of the project, the newest first, each with how many tasks it holds (whatever their labels)."""
    reader(access)
    return await sprints.sprints(conn, access.project_id)


async def sprint(conn: AsyncConnection, access: ProjectAccess, name: str) -> SprintRow:
    """Sprint ``name`` of the project, which the caller wrote in this transaction."""
    found = await sprints.sprint(conn, access.project_id, name)
    assert found is not None
    return found


async def project_members(conn: AsyncConnection, access: ProjectAccess) -> list[MemberRow]:
    """Everyone who holds a grant on the project, by login: whom a task may name (``project.members``)."""
    reader(access)
    return await repository.project_members(conn, access.project_id)


__all__ = ["all_sprints", "items", "project_members", "sprint", "task", "task_detail", "tasks"]
