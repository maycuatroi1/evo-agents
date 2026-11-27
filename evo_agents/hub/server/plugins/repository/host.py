"""The queries of the plugin host: the installed isolated plugins it runs, and the state of each backend it reports
(table plugin_backends, 0036)."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def installed_isolated(conn: AsyncConnection, name: str | None = None, *, lock: bool = False) -> list:
    p, v = tables.plugins, tables.plugin_versions
    query = (
        select(p.c.id, p.c.name, p.c.installed_version, v.c.manifest, v.c.sha256, v.c.size, v.c.tier)
        .join_from(p, v, (v.c.plugin_id == p.c.id) & (v.c.version == p.c.installed_version))
        .where(p.c.installed_version.is_not(None), p.c.disabled_reason.is_(None), v.c.tier == "isolated")
        .order_by(p.c.name)
    )
    if name is not None:
        query = query.where(p.c.name == name)
    if lock:
        query = query.with_for_update(of=p)
    return list(await conn.execute(query))


async def plugin_ids(conn: AsyncConnection) -> dict[str, int]:
    """plugin name -> its id, for every plugin of the store."""
    return {row.name: row.id for row in await conn.execute(select(tables.plugins.c.id, tables.plugins.c.name))}


async def put_backend(conn: AsyncConnection, values: dict) -> None:
    """Write the state of a backend as the host reported it now."""
    pb = tables.plugin_backends
    values = {**values, "reported_at": func.now()}
    await conn.execute(
        pg_insert(pb).values(**values).on_conflict_do_update(index_elements=[pb.c.plugin_id], set_=values)
    )


async def backend_row(conn: AsyncConnection, plugin_id: int):
    pb = tables.plugin_backends
    return (await conn.execute(select(pb).where(pb.c.plugin_id == plugin_id))).first()
