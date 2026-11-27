"""The query of the commands plugins add to the CLI (``service.commands``)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def installed_manifests(conn: AsyncConnection) -> list:
    """(name, installed_version, manifest) of every plugin installed and turned on, by name."""
    p, v = tables.plugins, tables.plugin_versions
    query = (
        select(p.c.name, p.c.installed_version, v.c.manifest)
        .join_from(p, v, (v.c.plugin_id == p.c.id) & (v.c.version == p.c.installed_version))
        .where(p.c.disabled_reason.is_(None))
        .order_by(p.c.name)
    )
    return list((await conn.execute(query)).all())
