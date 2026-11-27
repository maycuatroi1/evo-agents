"""The SQL of the worker on duty's graph routes (``evo_agents.hub.server.kg_worker``): the runs the hub holds of a
project, ingested and pending, and an ingested run's log."""

from __future__ import annotations

from sqlalchemy import Table, Text, cast, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def _run_ids(conn: AsyncConnection, runs: Table, project_id: int, source: str | None) -> list[str]:
    run_id = cast(runs.c.run_id, Text).label("run_id")
    query = select(run_id).where(runs.c.project_id == project_id)
    if source is not None:
        query = query.where(runs.c.source == source)
    return list((await conn.execute(query.order_by("run_id"))).scalars().all())


async def ingested(conn: AsyncConnection, project_id: int, source: str | None) -> list[str]:
    """The ids of the project's ingested runs, of one source unless ``source`` is None, in order."""
    return await _run_ids(conn, tables.kg_ingests, project_id, source)


async def pending(conn: AsyncConnection, project_id: int, source: str | None) -> list[str]:
    """The ids of the project's pending runs, of one source unless ``source`` is None, in order."""
    return await _run_ids(conn, tables.kg_pending_runs, project_id, source)


async def run_log(conn: AsyncConnection, project_id: int, run_id):
    """(source, log_sha256, log_size) of ingested run ``run_id`` of the project; None for none."""
    ingests = tables.kg_ingests
    return (
        await conn.execute(
            select(ingests.c.source, ingests.c.log_sha256, ingests.c.log_size).where(
                ingests.c.project_id == project_id, ingests.c.run_id == run_id
            )
        )
    ).one_or_none()
