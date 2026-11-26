"""The queries of a project's sprints (tasks_sprints): the sprints read (``sprints``, ``sprint``, ``states``), the
active one beside a sprint (``active_other``), a sprint kept or changed (``insert_sprint``, ``update_sprint``), and the
unfinished tasks of a closing sprint moved (``move_tasks``).
"""

from __future__ import annotations

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.tasks.models.rows import SprintRow


def _sprints_query(project_id: int):
    s, t, u = tables.tasks.sprints, tables.tasks.items, tables.users
    count = select(func.count()).where(t.c.sprint_id == s.c.id).scalar_subquery()
    return (
        select(
            s.c.id,
            s.c.name,
            s.c.goal,
            s.c.starts_on,
            s.c.ends_on,
            s.c.state,
            u.c.login.label("created_by"),
            s.c.created_at,
            s.c.closed_at,
            s.c.activated_at,
            count.label("tasks"),
        )
        .join_from(s, u, u.c.id == s.c.created_by)
        .where(s.c.project_id == project_id)
    )


async def sprints(conn: AsyncConnection, project_id: int) -> list[SprintRow]:
    """The sprints of the project, the newest first, each with how many tasks it holds (whatever their labels)."""
    s = tables.tasks.sprints
    query = _sprints_query(project_id).order_by(s.c.starts_on.desc().nulls_first(), s.c.id.desc())
    return [SprintRow(**row._mapping) for row in await conn.execute(query)]


async def sprint(conn: AsyncConnection, project_id: int, name: str, *, lock: bool = False) -> SprintRow | None:
    """The sprint of the project named ``name``, or None."""
    s = tables.tasks.sprints
    query = _sprints_query(project_id).where(s.c.name == name)
    if lock:
        query = query.with_for_update(of=s)
    row = (await conn.execute(query)).first()
    return SprintRow(**row._mapping) if row else None


async def states(conn: AsyncConnection, project_id: int) -> dict[str, str]:
    """name -> state of every sprint of the project."""
    s = tables.tasks.sprints
    query = select(s.c.name, s.c.state).where(s.c.project_id == project_id)
    return {row.name: row.state for row in await conn.execute(query)}


async def active_other(conn: AsyncConnection, project_id: int, sprint_id: int | None) -> str | None:
    """The name of the active sprint of the project other than ``sprint_id``, or None."""
    s = tables.tasks.sprints
    query = select(s.c.name).where(s.c.project_id == project_id, s.c.state == "active")
    if sprint_id is not None:
        query = query.where(s.c.id != sprint_id)
    return await conn.scalar(query)


def _stamped(values: dict, activated: bool, closed: bool) -> dict:
    stamps = {"activated_at": func.now()} if activated else {}
    return {**values, **stamps, **({"closed_at": func.now()} if closed else {})}


async def insert_sprint(conn: AsyncConnection, values: dict, *, activated: bool = False) -> None:
    """Keep a new sprint; ``activated`` it was made active now."""
    await conn.execute(insert(tables.tasks.sprints).values(**_stamped(values, activated, False)))


async def update_sprint(
    conn: AsyncConnection, sprint_id: int, values: dict, *, activated: bool = False, closed: bool = False
) -> None:
    """Set ``values`` of sprint ``sprint_id``; ``activated`` or ``closed`` it turned active or closed now."""
    s = tables.tasks.sprints
    await conn.execute(update(s).where(s.c.id == sprint_id).values(**_stamped(values, activated, closed)))


async def move_tasks(conn: AsyncConnection, sprint_id: int, statuses: list[str], target: int | None) -> list[int]:
    """Move the tasks of sprint ``sprint_id`` whose status is one of ``statuses`` to sprint ``target`` (None: to no
    sprint); their ids."""
    t = tables.tasks.items
    moving = (t.c.sprint_id == sprint_id) & t.c.status.in_(statuses)
    moved = await conn.execute(update(t).where(moving).values(sprint_id=target).returning(t.c.id))
    return list(moved.scalars().all())


__all__ = ["active_other", "insert_sprint", "move_tasks", "sprint", "sprints", "states", "update_sprint"]
