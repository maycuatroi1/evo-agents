"""The query of the pass of the ops queue (``ops.service.queue`` is the design): the projects it looks at."""

from __future__ import annotations

from sqlalchemy import select, union
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def candidates(conn: AsyncConnection) -> list[int]:
    """The projects a pass looks at, by id: those whose default system's spec turns ops on, and those whose queue has
    a cursor (to drop it once ops is off)."""
    s, c = tables.agent_systems, tables.ops_cursors
    on = select(s.c.project_id).where(s.c.is_default, s.c.spec["ops"]["enabled"].as_boolean().is_(True))
    return sorted((await conn.execute(union(on, select(c.c.project_id)))).scalars().all())
