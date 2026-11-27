"""The queries of the delegations of the records API (table plugin_record_leases), and the levels a project granted a
plugin's calls with no caller (``project_plugins.records_level``)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def add_lease(conn: AsyncConnection, values: dict, lasts: timedelta) -> None:
    """Write a lease that runs out ``lasts`` from now, the leases that ran out deleted first."""
    leases = tables.plugin_record_leases
    await conn.execute(delete(leases).where(leases.c.expires_at < func.now()))
    await conn.execute(insert(leases).values(**values, expires_at=func.now() + lasts))


async def live_lease(conn: AsyncConnection, token_hash: str, plugin_id: int, project_id: int):
    """The live lease whose hash is ``token_hash`` of the plugin in the project, or None."""
    lease = tables.plugin_record_leases.c
    query = select(tables.plugin_record_leases).where(
        lease.token_hash == token_hash,
        lease.plugin_id == plugin_id,
        lease.project_id == project_id,
        lease.expires_at > func.now(),
    )
    return (await conn.execute(query)).first()


async def records_level(conn: AsyncConnection, project_id: int, plugin_id: int) -> str | None:
    pp = tables.project_plugins.c
    query = select(pp.records_level).where(pp.project_id == project_id, pp.plugin_id == plugin_id)
    return (await conn.execute(query)).scalar_one_or_none()
