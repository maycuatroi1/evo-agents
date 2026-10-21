"""When the Loop of a project ticks (plan loop-engine, criterion a4): every 5 minutes, and on each of its events.

A tick is the job ``hub.loop_tick`` of one project, deferred with lock and queueing lock ``loop:<project id>``
(``jobs.loop_lock``): one tick of a project runs at a time, and at most one waits, which every event that comes
meanwhile joins. ``hub.loop_ticks`` queues one for each project whose Loop is on (loop_states.enabled), every 5
minutes; ``hub_events.record`` queues one in the transaction of each event of such a project (``on_event``), so the
tick exists exactly when the event does. A project whose Loop is off gets no tick at all.

The tick's code is the server's (``evo_agents.hub.server.loop.tick``); this module imports no server module, so
``hub_events`` and the worker's job list may import it (docs/structure.md, section Package boundaries).
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import jobs, tables

log = logging.getLogger(__name__)


async def is_on(conn: AsyncConnection, project_id: int) -> bool:
    """Whether project ``project_id``'s Loop is on."""
    s = tables.loop_states
    return bool(await conn.scalar(select(s.c.enabled).where(s.c.project_id == project_id)))


async def defer_tick(conn: AsyncConnection, project_id: int) -> int | None:
    """Queue a tick of project ``project_id``'s Loop in ``conn``'s transaction; its job id, or None when one waits
    already."""
    key = jobs.loop_lock(project_id)
    return await jobs.JobQueue.in_transaction().defer(
        jobs.LOOP_TICK, lock=key, queueing_lock=key, connection=conn, project_id=project_id
    )


async def on_event(conn: AsyncConnection, project_id: int) -> int | None:
    """Queue a tick for an event of project ``project_id`` written in ``conn``'s transaction, when its Loop is on."""
    if not await is_on(conn, project_id):
        return None
    return await defer_tick(conn, project_id)


async def queue_ticks(engine: AsyncEngine, manager) -> dict:
    """Queue a tick of each project whose Loop is on (the periodic ``hub.loop_ticks``), with ``manager`` (the job
    manager of the worker running it)."""
    s = tables.loop_states
    async with engine.begin() as conn:
        projects = list(await conn.scalars(select(s.c.project_id).where(s.c.enabled).order_by(s.c.project_id)))
    queue, queued = jobs.JobQueue(manager), 0
    for project_id in projects:
        key = jobs.loop_lock(project_id)
        if await queue.defer(jobs.LOOP_TICK, lock=key, queueing_lock=key, project_id=project_id) is not None:
            queued += 1
    log.info("loop ticks queued", extra={"projects": len(projects), "queued": queued})
    return {"projects": len(projects), "queued": queued}


__all__ = ["defer_tick", "is_on", "on_event", "queue_ticks"]
