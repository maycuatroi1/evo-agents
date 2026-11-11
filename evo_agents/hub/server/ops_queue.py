"""The pass of the queue of operations events (``evo_agents.hub.ops_queue`` describes the model), in the reaper's pass
every minute: it queues the operations events of each project whose default system turns ops on
(``ops_queue.take``, one transaction a project, a project another pass holds skipped), runs the rules of tier 0 on
what it queued (``ops_rules.tier0``: a run that failed because of its machine handed again, notices whose work is
done marked read) and closes the batches whose time came (``ops_queue.close_due``). It calls no model.
"""

from __future__ import annotations

import logging
from collections import Counter

from sqlalchemy import select, union
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import ops_queue, tables
from evo_agents.hub.server import ops_rules

log = logging.getLogger(__name__)

TIER0: tuple[ops_queue.Rule, ...] = ()  # rules of tier 0 that keep an event out of the queue; ops_rules works on items


def _candidates():
    """The projects a pass looks at: those whose default system's spec turns ops on, and those whose queue has a
    cursor (to drop it once ops is off)."""
    s, c = tables.agent_systems, tables.ops_cursors
    on = select(s.c.project_id).where(s.c.is_default, s.c.spec["ops"]["enabled"].as_boolean().is_(True))
    return union(on, select(c.c.project_id))


async def ops_pass(engine: AsyncEngine, rules: tuple[ops_queue.Rule, ...] | None = None) -> dict[str, int]:
    """Queue the operations events of each project whose ops is on and run tier 0 on its items (``ops_rules.tier0``),
    then close the batches whose time came. How many items it queued, what tier 0 did, and how many batches became
    ready and empty."""
    async with engine.connect() as conn:
        projects = sorted((await conn.execute(_candidates())).scalars().all())
    queued, tier0 = 0, Counter[str]()
    for project_id in projects:
        async with engine.begin() as conn:
            queued += await ops_queue.take(conn, project_id, TIER0 if rules is None else rules)
            tier0.update(await ops_rules.tier0(conn, project_id))
    async with engine.begin() as conn:
        closed = await ops_queue.close_due(conn)
    found = {"queued": queued, **{key: tier0[key] for key in ("redelivered", "orchestrated", "read")}, **closed}
    if any(found.values()):
        log.info("ops queue", extra=found)
    return found


__all__ = ["TIER0", "ops_pass"]
