"""The intakes of a project in the database (table intakes), and what a put writes besides: the pending proposals of
@builder (table builder_proposals) and the tasks of the plugin tasks (tables of ``tables.tasks``), through the tables,
as the plugin srs writes its frame tasks."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import intakes, tables


def _select():
    """An intake, with the logins of who asked for it and who put its result."""
    i, u = tables.intakes, tables.users
    by, put = u.alias("by"), u.alias("put")
    return (
        select(i, by.c.login.label("requested_login"), put.c.login.label("put_login"))
        .join(by, by.c.id == i.c.requested_by)
        .outerjoin(put, put.c.id == i.c.put_by)
    )


async def found(conn: AsyncConnection, project_id: int, ref: str, *, lock: bool = False):
    """Intake ``ref`` of the project (its row locked until the transaction ends with ``lock``); None without one."""
    i = tables.intakes
    query = _select().where(i.c.project_id == project_id, i.c.number == intakes.number_of(ref))
    if lock:
        query = query.with_for_update(of=i)
    return (await conn.execute(query)).first()


async def listed(conn: AsyncConnection, project_id: int, status: str | None = None) -> list:
    """The project's intakes, newest first; those of ``status`` alone when given."""
    i = tables.intakes
    query = _select().where(i.c.project_id == project_id)
    if status is not None:
        query = query.where(i.c.status == status)
    return list((await conn.execute(query.order_by(i.c.number.desc()))).all())


async def _next_number(conn: AsyncConnection, table, project_id: int) -> int:
    query = select(func.coalesce(func.max(table.c.number), 0)).where(table.c.project_id == project_id)
    return int(await conn.scalar(query) or 0) + 1


async def next_intake(conn: AsyncConnection, project_id: int) -> int:
    """The number of the project's next intake."""
    return await _next_number(conn, tables.intakes, project_id)


async def next_problem(conn: AsyncConnection, project_id: int) -> int:
    """The number the project's next problem takes."""
    return await _next_number(conn, tables.problems, project_id)


async def next_task(conn: AsyncConnection, project_id: int) -> int:
    """The number the project's next task takes."""
    return await _next_number(conn, tables.tasks.items, project_id)


async def start(conn: AsyncConnection, values: dict) -> None:
    """Write a new intake."""
    await conn.execute(insert(tables.intakes).values(values))


async def done(conn: AsyncConnection, intake_id: int, stamp: dict) -> None:
    """Mark intake ``intake_id`` done, with its result and what the hub made of it."""
    i = tables.intakes
    await conn.execute(update(i).where(i.c.id == intake_id).values({**stamp, "done_at": func.now()}))


async def members(conn: AsyncConnection, project_id: int, logins: set[str]) -> dict[str, int]:
    """login -> user id of each of ``logins`` who holds a grant on the project."""
    if not logins:
        return {}
    u, g = tables.users, tables.grants
    query = (
        select(u.c.login, u.c.id)
        .join_from(u, g, g.c.user_id == u.c.id)
        .where(g.c.project_id == project_id, u.c.login.in_(sorted(logins)))
    )
    return {row.login: row.id for row in await conn.execute(query)}


async def evidence_sources(conn: AsyncConnection, problem_id: int) -> list:
    """The source kind and address of each line of evidence of problem ``problem_id``."""
    e = tables.problem_evidence
    query = select(e.c.source_kind, e.c.source_uri).where(e.c.problem_id == problem_id)
    return [row._mapping for row in await conn.execute(query)]


async def confirmed_evidence(conn: AsyncConnection, problem_id: int) -> bool:
    """Whether a member confirmed a line of evidence of problem ``problem_id``."""
    e = tables.problem_evidence
    query = select(func.count()).where(e.c.problem_id == problem_id, e.c.confirmed_by.is_not(None))
    return bool(await conn.scalar(query))


async def propose(conn: AsyncConnection, values: dict) -> int:
    """Write a pending proposal of @builder, with no chat; its id."""
    b = tables.builder_proposals
    return (await conn.execute(insert(b).values(values).returning(b.c.id))).scalar_one()


async def add_task(conn: AsyncConnection, values: dict) -> Any:
    """Write a task of the plugin tasks; its id."""
    t = tables.tasks.items
    return await conn.scalar(insert(t).values(**values).returning(t.c.id))


async def task_history(conn: AsyncConnection, task_id: int, actor: int, name: str, new: str, cause: str) -> None:
    """Write a line of the history of task ``task_id``: field ``name`` set to ``new``."""
    values = {"task_id": task_id, "actor_id": actor, "field": name, "from_value": None, "to_value": new, "cause": cause}
    await conn.execute(insert(tables.tasks.events).values(**values))


async def assign(conn: AsyncConnection, task_id: int, user_id: int) -> None:
    """Make member ``user_id`` the assignee of task ``task_id``."""
    await conn.execute(insert(tables.tasks.people).values(task_id=task_id, role="assignee", user_id=user_id))
