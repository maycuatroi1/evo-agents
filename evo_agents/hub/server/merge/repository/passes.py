"""The query of an admin's pass of a hold of the hub's merge queue in the ledger of its merge request
(``service.passes``)."""

from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.merge.models.passes import PERSON, Pass


async def standing(conn: AsyncConnection, row_id: int, gate: int) -> Pass | None:
    """The newest admin's pass of the hold of merge request ``row_id`` at ``gate`` that still holds: no head came after
    it; None without one."""
    e = tables.merge_events
    query = (
        select(e.c.id, e.c.sha, e.c.reason, e.c.detail)
        .where(
            e.c.queue_id == row_id,
            e.c.kind == "gate",
            e.c.gate == gate,
            e.c.passed.is_(True),
            e.c.detail.has_key(PERSON),
        )
        .order_by(e.c.id.desc())
        .limit(1)
    )
    found = (await conn.execute(query)).first()
    if found is None:
        return None
    later = exists().where(e.c.queue_id == row_id, e.c.kind == "head", e.c.id > found.id)
    if (await conn.execute(select(later))).scalar_one():
        return None
    return Pass(found.id, found.sha, str(found.detail.get(PERSON)), found.reason or "")
