"""The SQL of installing from a market: the bundle's blob row, the project a self-managed project's plugin belongs
to, and the install requests of the projects."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def add_bundle_blob(conn: AsyncConnection, sha256: str, size: int, user_id: int) -> None:
    """The blob row of a plugin bundle of the hub's, kept once."""
    blobs = tables.blobs
    await conn.execute(
        pg_insert(blobs)
        .values(project_id=None, sha256=sha256, size=size, kind="plugin-bundle", created_by=user_id)
        .on_conflict_do_nothing()
    )


async def set_plugin_project(conn: AsyncConnection, name: str, project_id: int) -> None:
    plugins = tables.plugins
    await conn.execute(update(plugins).where(plugins.c.name == name).values(project_id=project_id))


async def close_requests(conn: AsyncConnection, user_id: int, name: str) -> None:
    """Mark the open install requests of plugin ``name`` installed, by ``user_id``."""
    r = tables.market_install_requests
    await conn.execute(
        update(r)
        .where(r.c.plugin == name, r.c.state == "open")
        .values(state="installed", decided_by=user_id, decided_at=func.now())
    )


async def add_request(conn: AsyncConnection, project_id: int, source: int, plugin: str, user_id: int):
    """The (id, state) of the request added; None when the project has one open already."""
    r = tables.market_install_requests
    added = await conn.execute(
        pg_insert(r)
        .values(project_id=project_id, source_id=source, plugin=plugin, requested_by=user_id)
        .on_conflict_do_nothing()
        .returning(r.c.id, r.c.state)
    )
    return added.first()


async def open_request(conn: AsyncConnection, project_id: int, source: int, plugin: str):
    r = tables.market_install_requests
    held = select(r.c.id, r.c.state).where(
        r.c.project_id == project_id, r.c.source_id == source, r.c.plugin == plugin, r.c.state == "open"
    )
    return (await conn.execute(held)).first()
