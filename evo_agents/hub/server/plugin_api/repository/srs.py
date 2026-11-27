"""The query of the use cases of a version of an SRS document (``service.srs``)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.plugin_api.models.srs import USE_CASE, ApprovedUseCase


async def use_cases(conn: AsyncConnection, version_id: int) -> list[ApprovedUseCase]:
    """The use cases of version ``version_id``, in the document's order: code and key of each."""
    s = tables.srs
    iv, i = s.item_versions, s.items
    query = (
        select(i.c.code, i.c.key)
        .join_from(iv, i, i.c.id == iv.c.item_id)
        .where(iv.c.version_id == version_id, i.c.kind == USE_CASE, i.c.code.is_not(None))
        .order_by(iv.c.position)
    )
    return [ApprovedUseCase(code=row.code, key=row.key) for row in await conn.execute(query)]


__all__ = ["use_cases"]
