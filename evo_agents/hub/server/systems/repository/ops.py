"""The ops queue of a project as the routes of the ops read it (``evo_agents.hub.ops_queue``): its batches by state,
and its batches with their items and threads."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import ops_queue, tables


async def counts(conn: AsyncConnection, project_id: int) -> dict[str, int]:
    """The batches of the project's queue by state, each state of ``ops_queue.BATCH_STATES`` named."""
    b = tables.ops_batches
    query = select(b.c.state, func.count()).where(b.c.project_id == project_id).group_by(b.c.state)
    found = {state: 0 for state in ops_queue.BATCH_STATES}
    found.update({state: n for state, n in await conn.execute(query)})
    return found


async def batches(conn: AsyncConnection, project_id: int, state: str | None, limit: int) -> list[dict]:
    """The batches of the project's queue, the newest first, in ``state`` when given: each row of ops_batches with
    ``items``, ``pending`` and the ``chat_id`` and ``root_message_id`` of its thread."""
    b, i, t = tables.ops_batches, tables.ops_items, tables.orchestrator_threads
    items = select(func.count()).where(i.c.batch_id == b.c.id).scalar_subquery()
    pending = select(func.count()).where(i.c.batch_id == b.c.id, i.c.state == "pending").scalar_subquery()
    query = (
        select(b, items.label("items"), pending.label("pending"), t.c.chat_id, t.c.root_message_id)
        .select_from(b.outerjoin(t, t.c.id == b.c.thread_id))
        .where(b.c.project_id == project_id)
    )
    if state is not None:
        query = query.where(b.c.state == state)
    return [dict(row._mapping) for row in await conn.execute(query.order_by(b.c.id.desc()).limit(limit))]
