"""The pass of the queue of operations events (``evo_agents.hub.ops_queue`` describes the model), in the reaper's pass
every minute: it queues the operations events of each project whose default system turns ops on
(``ops_queue.take``, one transaction a project, a project another pass holds skipped), runs the rules of tier 0 on
what it queued (``ops.service.rules.tier0``: a run that failed because of its machine handed again, notices whose work is
done marked read), writes the events of stalled plans and of chat runs queued too long that the next pass queues
(``ops_stalls.check``), and closes the batches whose time came (``ops_queue.close_due``). It calls no model. Then it opens
the thread of each ready batch to the Orchestrator of the project's default system and settles the batches whose
thread ended (``ops.service.threads.threads_pass``).
"""

from __future__ import annotations

import logging
from collections import Counter

from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import ops_queue, ops_stalls
from evo_agents.hub.server.ops.repository import queue as store
from evo_agents.hub.server.ops.service import rules as ops_rules
from evo_agents.hub.server.ops.service import threads as ops_threads

log = logging.getLogger(__name__)

TIER0: tuple[ops_queue.Rule, ...] = ()  # rules of tier 0 that keep an event out of the queue; ops_rules works on items


async def ops_pass(engine: AsyncEngine, rules: tuple[ops_queue.Rule, ...] | None = None) -> dict[str, int]:
    """Queue the operations events of each project whose ops is on and run tier 0 on its items (``ops.service.rules.tier0``),
    then close the batches whose time came. How many items it queued, what tier 0 did, and how many batches became
    ready and empty."""
    async with engine.connect() as conn:
        projects = await store.candidates(conn)
    queued, tier0, stalls = 0, Counter[str](), Counter[str]()
    for project_id in projects:
        async with engine.begin() as conn:
            queued += await ops_queue.take(conn, project_id, TIER0 if rules is None else rules)
            tier0.update(await ops_rules.tier0(conn, project_id))
            stalls.update(await ops_stalls.check(conn, project_id))
    async with engine.begin() as conn:
        closed = await ops_queue.close_due(conn)
    found = {"queued": queued, **{key: tier0[key] for key in ("redelivered", "orchestrated", "read")}, **closed}
    found.update({key: stalls[key] for key in ("stalled", "queued_long")})
    threads = await ops_threads.threads_pass(engine)  # logged, not returned: what ops_threads did is its own
    if any(found.values()):
        log.info("ops queue", extra=found)
    if threads:
        log.info("ops threads", extra={f"threads_{key}": count for key, count in threads.items()})
    return found


__all__ = ["TIER0", "ops_pass"]
