"""The SQL of the agent packs: the versions of a plugin the hub holds."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


def _versions(plugin_id: int):
    v = tables.plugin_versions
    return select(v.c.version, v.c.manifest, v.c.r2_key, v.c.sha256, v.c.size).where(v.c.plugin_id == plugin_id)


async def versions(conn: AsyncConnection, plugin_id: int) -> list:
    """(version, manifest, r2_key, sha256, size) of each version of the plugin the hub holds."""
    return list(await conn.execute(_versions(plugin_id)))


async def version(conn: AsyncConnection, plugin_id: int, version: str):
    """The row of ``versions`` of version ``version`` of the plugin; None when the hub holds none."""
    query = _versions(plugin_id).where(tables.plugin_versions.c.version == version)
    return (await conn.execute(query)).one_or_none()
