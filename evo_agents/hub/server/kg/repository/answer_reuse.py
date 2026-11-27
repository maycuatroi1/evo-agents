"""The SQL of the daily signal of the confirmed answers loaded (``evo_agents.hub.server.kg.service.answer_reuse``):
the projects, the first admin of one, and its ingest key INGEST_KEY."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import signals, tables
from evo_agents.hub.server.kg.models.answer_reuse import INGEST_KEY


async def projects(conn: AsyncConnection) -> Sequence[Any]:
    """(id, name) of every project."""
    return (await conn.execute(select(tables.projects.c.id, tables.projects.c.name))).all()


async def first_admin(conn: AsyncConnection, project_id: int) -> int | None:
    g = tables.grants
    return await conn.scalar(
        select(g.c.user_id).where(g.c.project_id == project_id, g.c.role == "admin").order_by(g.c.user_id).limit(1)
    )


async def ingest_keys(conn: AsyncConnection, project_id: int) -> Sequence[Any]:
    """(id, max_level, revoked_at) of the project's ingest keys named INGEST_KEY."""
    k = tables.signal_ingest_keys
    return (
        await conn.execute(
            select(k.c.id, k.c.max_level, k.c.revoked_at).where(k.c.project_id == project_id, k.c.name == INGEST_KEY)
        )
    ).all()


async def add_ingest_key(conn: AsyncConnection, project_id: int, max_level: str | None, owner: int):
    """A new ingest key INGEST_KEY of the project, owned by ``owner``: its (id, max_level)."""
    k = tables.signal_ingest_keys
    return (
        await conn.execute(
            insert(k)
            .values(
                project_id=project_id,
                name=INGEST_KEY,
                max_level=max_level,
                token_hash=signals.hash_token(signals.new_ingest_token()),  # nobody reads it: the hub writes itself
                created_by=owner,
            )
            .returning(k.c.id, k.c.max_level)
        )
    ).one()
