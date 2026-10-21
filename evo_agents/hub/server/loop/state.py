"""Turning a project's Loop on and off (plan loop-engine, criteria a4 and a11), in the transaction of the policy
revision that sets ``loop.enabled``.

On: the project's row of loop_states is on, with when, and its cursor at the newest event of hub_events the project
has then, so the Loop reads only what comes after (no backfill; a chain from before enters with a baseline a writer
opens at once), and a tick is queued. Off: the row is off and every work item of the project not finished is
cancelled with reason ``loop_off``; runs in flight finish on their own and make nothing more. A project whose Loop
was never on has no row, and a revision that leaves ``loop.enabled`` as it was changes nothing here.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import loop_jobs, tables
from evo_agents.hub.server.loop.model import CANCELLED, CANCELLED_EVENT, FINISHED

log = logging.getLogger(__name__)


async def enabled(conn: AsyncConnection, project_id: int, *, lock: bool = False) -> bool:
    """Whether project ``project_id``'s Loop is on; with ``lock``, its row of loop_states held until the transaction
    ends."""
    s = tables.loop_states
    query = select(s.c.enabled).where(s.c.project_id == project_id)
    return bool(await conn.scalar(query.with_for_update() if lock else query))


async def cancel_open(conn: AsyncConnection, project_id: int, reason: str, **detail) -> int:
    """Cancel every work item of the project not finished, with ``reason``; how many."""
    i, e = tables.loop_items, tables.loop_item_events
    values = {"state": CANCELLED, "reason": reason, "updated_at": func.now(), "finished_at": func.now()}
    query = update(i).where(i.c.project_id == project_id, i.c.state.not_in(FINISHED)).values(values)
    ids = list(await conn.scalars(query.returning(i.c.id)))
    if ids:
        rows = [{"item_id": item_id, "kind": CANCELLED_EVENT, "state": CANCELLED, "reason": reason} for item_id in ids]
        await conn.execute(insert(e).values([{**row, "detail": dict(detail)} for row in rows]))
    return len(ids)


async def policy_written(conn: AsyncConnection, project_id: int, on: bool) -> None:
    """Put the project's Loop on or off as the policy revision just written sets it (see the module)."""
    if await enabled(conn, project_id, lock=True) == on:
        return
    s, h = tables.loop_states, tables.hub_events
    if on:
        newest = select(func.coalesce(func.max(h.c.id), 0)).where(h.c.project_id == project_id).scalar_subquery()
        values = {"enabled": True, "enabled_at": func.now(), "cursor": newest, "updated_at": func.now()}
        upsert = pg_insert(s).values(project_id=project_id, **values)
        await conn.execute(upsert.on_conflict_do_update(index_elements=[s.c.project_id], set_=values))
        await loop_jobs.defer_tick(conn, project_id)
        log.info("loop on", extra={"project_id": project_id})
        return
    await conn.execute(update(s).where(s.c.project_id == project_id).values(enabled=False, updated_at=func.now()))
    cancelled = await cancel_open(conn, project_id, "loop_off")
    log.info("loop off", extra={"project_id": project_id, "cancelled": cancelled})


__all__ = ["cancel_open", "enabled", "policy_written"]
