"""The SQL of the markets: the plugins the hub's store holds, the market sources with who added them, and adding,
sealing and removing a source."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.plugins.models.store import IMAGE


async def installed_plugins(conn: AsyncConnection) -> dict[str, dict]:
    """name -> {installed_version, publisher, image} of every plugin the hub's store holds."""
    p, v = tables.plugins, tables.plugin_versions
    image = func.bool_or(v.c.signature_kind == IMAGE)
    query = (
        select(p.c.name, p.c.installed_version, p.c.publisher, p.c.disabled_reason, image.label("image"))
        .join_from(p, v, v.c.plugin_id == p.c.id, isouter=True)
        .group_by(p.c.id)
    )
    return {row.name: row._asdict() for row in await conn.execute(query)}


async def source_rows(conn: AsyncConnection, source_id: int | None = None) -> Sequence[Any]:
    s, users = tables.market_sources, tables.users
    query = (
        select(s, users.c.login)
        .join_from(s, users, users.c.id == s.c.added_by, isouter=True)
        .order_by(s.c.builtin.desc(), s.c.id)
    )
    if source_id is not None:
        query = query.where(s.c.id == source_id)
    return (await conn.execute(query)).all()


async def add_source(conn: AsyncConnection, repo: str, ref: str, path: str, user_id: int) -> int | None:
    """The id of the source added; None when the hub reads that path of that repo at that ref already."""
    s = tables.market_sources
    added = await conn.execute(
        pg_insert(s).values(repo=repo, ref=ref, path=path, added_by=user_id).on_conflict_do_nothing().returning(s.c.id)
    )
    return added.scalar()


async def set_token(conn: AsyncConnection, source_id: int, sealed) -> None:
    s = tables.market_sources
    await conn.execute(s.update().where(s.c.id == source_id).values(token=sealed))


async def delete_source(conn: AsyncConnection, source_id: int) -> None:
    s = tables.market_sources
    await conn.execute(delete(s).where(s.c.id == source_id))
