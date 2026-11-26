"""The queries of the tasks of a project (tasks_items, tasks_people): the tasks a list keeps (``tasks``, with
``Filters``), every task (``all_tasks``), one task (``task``), the people in each slot (``people``), the members a task
may name (``project_members``, ``members``), and a task kept or changed (``insert_task``, ``update_task``,
``replace_people``). Whether a member sees a task is the service's to say: these read every row of the project.
"""

from __future__ import annotations

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.tasks.models.rows import MemberRow, People, TaskRow
from evo_agents.hub.server.tasks.models.values import Filters
from evo_agents.hub.server.tasks.repository.blockers import blocked


def _tasks_query(project_id: int):
    t, s, u = tables.tasks.items, tables.tasks.sprints, tables.users
    parent = t.alias("parent")
    return (
        select(
            t.c.id,
            t.c.number,
            t.c.type,
            t.c.title,
            t.c.description_md,
            t.c.outcome_md,
            t.c.status,
            t.c.priority,
            t.c.due_on,
            s.c.name.label("sprint"),
            parent.c.number.label("parent_number"),
            t.c.label,
            t.c.external_ref,
            u.c.login.label("created_by"),
            t.c.created_at,
            t.c.updated_at,
        )
        .select_from(
            t.join(u, u.c.id == t.c.created_by)
            .outerjoin(s, s.c.id == t.c.sprint_id)
            .outerjoin(parent, parent.c.id == t.c.parent_id)
        )
        .where(t.c.project_id == project_id)
    )


def _filtered(query, filters: Filters):
    t, s, p, u = tables.tasks.items, tables.tasks.sprints, tables.tasks.people, tables.users
    if filters.status:
        query = query.where(t.c.status == filters.status)
    if filters.backlog:
        query = query.where(t.c.sprint_id.is_(None))
    elif filters.sprint:
        query = query.where(s.c.name == filters.sprint)
    if filters.member_id is not None:
        query = query.where(select(p.c.task_id).where(p.c.task_id == t.c.id, p.c.user_id == filters.member_id).exists())
    if filters.blocked:
        query = query.where(blocked())
    if filters.assignee:
        held = and_(p.c.task_id == t.c.id, p.c.role == "assignee", u.c.login == filters.assignee)
        query = query.where(select(p.c.task_id).join(u, u.c.id == p.c.user_id).where(held).exists())
    return query


async def all_tasks(conn: AsyncConnection, project_id: int) -> list[TaskRow]:
    """Every task of the project, by number."""
    t = tables.tasks.items
    return [TaskRow(**row._mapping) for row in await conn.execute(_tasks_query(project_id).order_by(t.c.number))]


async def tasks(conn: AsyncConnection, project_id: int, filters: Filters) -> list[TaskRow]:
    """Every task of the project that ``filters`` keeps, by number, whatever its label."""
    query = _filtered(_tasks_query(project_id), filters).order_by(tables.tasks.items.c.number)
    return [TaskRow(**row._mapping) for row in await conn.execute(query)]


async def task(conn: AsyncConnection, project_id: int, number: int, *, lock: bool = False) -> TaskRow | None:
    """Task ``number`` of the project, or None. ``lock`` takes its row lock first, for a write."""
    t = tables.tasks.items
    if lock:
        held = select(t.c.id).where(t.c.project_id == project_id, t.c.number == number).with_for_update()
        await conn.execute(held)
    row = (await conn.execute(_tasks_query(project_id).where(t.c.number == number))).first()
    return TaskRow(**row._mapping) if row else None


async def task_label(conn: AsyncConnection, project_id: int, number: int, *, lock: bool = False):
    """(id, parent_id, label, external_key) of task ``number`` of the project, its row locked with ``lock``; None
    when there is none."""
    t = tables.tasks.items
    query = select(t.c.id, t.c.parent_id, t.c.label, t.c.external_key).where(
        t.c.project_id == project_id, t.c.number == number
    )
    return (await conn.execute(query.with_for_update() if lock else query)).first()


async def parent_of(conn: AsyncConnection, task_id: int):
    """(id, parent_id) of task ``task_id``, or None."""
    t = tables.tasks.items
    return (await conn.execute(select(t.c.id, t.c.parent_id).where(t.c.id == task_id))).first()


async def children(conn: AsyncConnection, parent_id: int) -> list:
    """(number, title) of every child of task ``parent_id``."""
    t = tables.tasks.items
    return list(await conn.execute(select(t.c.number, t.c.title).where(t.c.parent_id == parent_id)))


async def task_id(conn: AsyncConnection, project_id: int, number: int) -> int:
    t = tables.tasks.items
    query = select(t.c.id).where(t.c.project_id == project_id, t.c.number == number)
    return (await conn.execute(query)).scalar_one()


async def imported_before(conn: AsyncConnection, project_id: int, keys: list[str]) -> dict[str, int]:
    """external key -> the number of the task of the project that holds it, for each of ``keys`` one holds."""
    if not keys:
        return {}
    t = tables.tasks.items
    query = select(t.c.external_key, t.c.number).where(t.c.project_id == project_id, t.c.external_key.in_(keys))
    return {row.external_key: row.number for row in await conn.execute(query)}


async def next_number(conn: AsyncConnection, project_id: int) -> int:
    t = tables.tasks.items
    last = await conn.scalar(select(func.max(t.c.number)).where(t.c.project_id == project_id))
    return (last or 0) + 1


async def insert_task(conn: AsyncConnection, values: dict) -> int:
    t = tables.tasks.items
    return (await conn.execute(insert(t).values(**values).returning(t.c.id))).scalar_one()


async def update_task(conn: AsyncConnection, task_id: int, columns: dict) -> None:
    """Set ``columns`` of task ``task_id``, and the time it changed."""
    t = tables.tasks.items
    await conn.execute(update(t).where(t.c.id == task_id).values(**columns, updated_at=func.now()))


async def set_parent(conn: AsyncConnection, project_id: int, number: int, parent_id: int) -> None:
    t = tables.tasks.items
    child = (t.c.project_id == project_id) & (t.c.number == number)
    await conn.execute(update(t).where(child).values(parent_id=parent_id))


async def linked_to_step(conn: AsyncConnection, project_id: int, plan_id: str, step: str) -> list:
    """(id, number, status) of every task of the project that links step ``step`` of plan ``plan_id``, by number."""
    t, k = tables.tasks.items, tables.tasks.links
    query = (
        select(t.c.id, t.c.number, t.c.status)
        .join_from(k, t, t.c.id == k.c.task_id)
        .where(t.c.project_id == project_id, k.c.kind == "plan_step", k.c.plan_id == plan_id, k.c.step_id == step)
        .order_by(t.c.number)
        .distinct()
    )
    return list(await conn.execute(query))


# People


async def people(conn: AsyncConnection, task_ids: list[int]) -> dict[int, People]:
    """task id -> who stands in each of its slots, each slot by login."""
    p, u = tables.tasks.people, tables.users
    query = (
        select(p.c.task_id, p.c.role, u.c.login)
        .join_from(p, u, u.c.id == p.c.user_id)
        .where(p.c.task_id.in_(task_ids or [0]))
        .order_by(p.c.task_id, p.c.role, u.c.login)
    )
    found = {task_id: People() for task_id in task_ids}
    for row in await conn.execute(query):
        slot = found[row.task_id]
        (slot.assignees if row.role == "assignee" else slot.roles[row.role]).append(row.login)
    return found


async def replace_people(conn: AsyncConnection, task_id: int, role: str, user_ids: list[int]) -> None:
    """The people of slot ``role`` of task ``task_id`` are ``user_ids`` now."""
    p = tables.tasks.people
    await conn.execute(delete(p).where(p.c.task_id == task_id, p.c.role == role))
    if user_ids:
        await conn.execute(insert(p), [{"task_id": task_id, "role": role, "user_id": user_id} for user_id in user_ids])


async def project_members(conn: AsyncConnection, project_id: int) -> list[MemberRow]:
    """Everyone who holds a grant on the project, by login."""
    u, g = tables.users, tables.grants
    query = (
        select(u.c.login, g.c.role)
        .join_from(u, g, g.c.user_id == u.c.id)
        .where(g.c.project_id == project_id)
        .order_by(u.c.login)
    )
    return [MemberRow(row.login, row.role) for row in await conn.execute(query)]


async def members(conn: AsyncConnection, project_id: int, logins: set[str]) -> dict[str, int]:
    """login -> user id of each of ``logins`` who holds a grant on the project."""
    u, g = tables.users, tables.grants
    query = (
        select(u.c.login, u.c.id)
        .join_from(u, g, g.c.user_id == u.c.id)
        .where(g.c.project_id == project_id, u.c.login.in_(sorted(logins)))
    )
    return {row.login: row.id for row in await conn.execute(query)}


__all__ = [
    "all_tasks",
    "children",
    "imported_before",
    "insert_task",
    "linked_to_step",
    "members",
    "next_number",
    "parent_of",
    "people",
    "project_members",
    "replace_people",
    "set_parent",
    "task",
    "task_id",
    "task_label",
    "tasks",
    "update_task",
]
