"""The SQL of the entities step's setup (``evo_agents.hub.server.kg_entities_setup``) beyond the KG settings and the
builds: the project a run belongs to."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def run_project(conn: AsyncConnection, run_id: int) -> int | None:
    """The project of run ``run_id``; None for no such run."""
    return (await conn.execute(select(tables.runs.c.project_id).where(tables.runs.c.id == run_id))).scalar()
