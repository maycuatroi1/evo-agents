"""The queries of a task's history (tasks_events) and comments (tasks_comments): a row of the history kept
(``record``, or ``record_delivered`` for a row of the hub written once for each delivery of an event), the history
and the comments of a task read (``events``, ``comments``), and a comment kept (``add_comment``).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.tasks.models.rows import CommentRow, EventRow
from evo_agents.hub.tables_tasks import MAX_VALUE_CHARS


def _text(value) -> str | None:
    if value is None:
        return None
    return str(value)[:MAX_VALUE_CHARS]


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


async def record_delivered(
    conn: AsyncConnection, task_id: int, name: str, old, new, cause: str, delivery: int | None
) -> bool:
    """Write a row of the hub into the history of task ``task_id``; False when this delivery wrote it already."""
    e = tables.tasks.events
    values = {"task_id": task_id, "field": name, "from_value": old, "to_value": new, "cause": cause}
    statement = pg_insert(e).values(**values, delivery_id=delivery).on_conflict_do_nothing().returning(e.c.id)
    return (await conn.execute(statement)).first() is not None


async def events(conn: AsyncConnection, task_id: int) -> list[EventRow]:
    """The history of a task, oldest first."""
    e, u = tables.tasks.events, tables.users
    query = (
        select(u.c.login.label("actor"), e.c.field, e.c.from_value, e.c.to_value, e.c.cause, e.c.at)
        .select_from(e.outerjoin(u, u.c.id == e.c.actor_id))
        .where(e.c.task_id == task_id)
        .order_by(e.c.at, e.c.id)
    )
    return [EventRow(**row._mapping) for row in await conn.execute(query)]


async def comments(conn: AsyncConnection, task_id: int) -> list[CommentRow]:
    """The comments on a task, oldest first."""
    c, u = tables.tasks.comments, tables.users
    query = (
        select(c.c.id, u.c.login.label("author"), c.c.body, c.c.created_at)
        .join_from(c, u, u.c.id == c.c.author_id)
        .where(c.c.task_id == task_id)
        .order_by(c.c.id)
    )
    return [CommentRow(**row._mapping) for row in await conn.execute(query)]


async def add_comment(conn: AsyncConnection, task_id: int, author_id: int, body: str) -> int:
    c = tables.tasks.comments
    values = {"task_id": task_id, "author_id": author_id, "body": body}
    return (await conn.execute(insert(c).values(**values).returning(c.c.id))).scalar_one()


__all__ = ["add_comment", "comments", "events", "record", "record_delivered"]
