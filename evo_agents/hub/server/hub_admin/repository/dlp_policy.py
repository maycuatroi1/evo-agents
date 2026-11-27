"""The SQL of a project's data loss prevention policy (``evo_agents.hub.server.dlp_policy``): the policy, its counts,
and setting it."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import tables


def reading(engine: AsyncEngine) -> AbstractAsyncContextManager[AsyncConnection]:
    """A connection that reads, outside any transaction of a write."""
    return engine.connect()


async def policy_rules(conn: AsyncConnection, project_id: int):
    """(mode, kinds, keywords) of the policy of project ``project_id``, or None when its admin set none."""
    d = tables.dlp_policies
    return (await conn.execute(select(d.c.mode, d.c.kinds, d.c.keywords).where(d.c.project_id == project_id))).first()


async def add_counts(conn: AsyncConnection, project_id: int, surface: str, action: str, counts) -> None:
    """Add ``counts`` (by rule kind) to dlp_counts of ``project_id``, ``surface`` and ``action``."""
    c = tables.dlp_counts
    for kind, number in sorted(dict(counts).items()):
        keys = {"project_id": project_id, "kind": kind, "surface": surface, "action": action}
        statement = pg_insert(c).values(**keys, count=number)
        await conn.execute(
            statement.on_conflict_do_update(
                index_elements=[c.c.project_id, c.c.kind, c.c.surface, c.c.action],
                set_={"count": c.c.count + statement.excluded.count, "last_at": func.now()},
            )
        )


async def policy_row(conn: AsyncConnection, project_id: int):
    """The dlp_policies row of project ``project_id``, or None."""
    d = tables.dlp_policies
    return (await conn.execute(select(d).where(d.c.project_id == project_id))).first()


async def count_rows(conn: AsyncConnection, project_id: int):
    """(kind, surface, action, count) of what the policy of project ``project_id`` redacted and blocked."""
    c = tables.dlp_counts
    return (
        await conn.execute(
            select(c.c.kind, c.c.surface, c.c.action, c.c.count)
            .where(c.c.project_id == project_id)
            .order_by(c.c.kind, c.c.surface, c.c.action)
        )
    ).all()


async def put_policy(conn: AsyncConnection, project_id: int, values: dict) -> None:
    """Set the policy of project ``project_id`` to ``values``."""
    d = tables.dlp_policies
    statement = pg_insert(d).values(project_id=project_id, **values)
    await conn.execute(
        statement.on_conflict_do_update(index_elements=[d.c.project_id], set_={**values, "updated_at": func.now()})
    )
