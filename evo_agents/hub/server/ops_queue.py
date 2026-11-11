"""The pass of the queue of operations events (``evo_agents.hub.ops_queue`` describes the model), in the reaper's pass
every minute: it queues the operations events of each project whose default system turns ops on
(``ops_queue.take``, one transaction a project, a project another pass holds skipped) and closes the batches whose
time came (``ops_queue.close_due``). It calls no model.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, union
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import ops_queue, tables

log = logging.getLogger(__name__)

TIER0: tuple[ops_queue.Rule, ...] = ()  # the rules of tier 0, which a later step of the plan adds


def _candidates():
    """The projects a pass looks at: those whose default system's spec turns ops on, and those whose queue has a
    cursor (to drop it once ops is off)."""
    s, c = tables.agent_systems, tables.ops_cursors
    on = select(s.c.project_id).where(s.c.is_default, s.c.spec["ops"]["enabled"].as_boolean().is_(True))
    return union(on, select(c.c.project_id))


async def ops_pass(engine: AsyncEngine, rules: tuple[ops_queue.Rule, ...] | None = None) -> dict[str, int]:
    """Queue the operations events of each project whose ops is on, then close the batches whose time came. How many
    items it queued and how many batches became ready and empty."""
    async with engine.connect() as conn:
        projects = sorted((await conn.execute(_candidates())).scalars().all())
    queued = 0
    for project_id in projects:
        async with engine.begin() as conn:
            queued += await ops_queue.take(conn, project_id, TIER0 if rules is None else rules)
    async with engine.begin() as conn:
        closed = await ops_queue.close_due(conn)
    if queued or any(closed.values()):
        log.info("ops queue", extra={"queued": queued, **closed})
    return {"queued": queued, **closed}


__all__ = ["TIER0", "ops_pass"]
