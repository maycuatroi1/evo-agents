"""Which run holds a browser identity: the one a worker holds, from its claim until it leaves the held states.

A run dispatched with identities (``runs.browser_identities``) is claimed only while none of them is held
(``runs.service.browsers.claim_gate``); ``follow`` is what ``run_state.move_run`` does with them on every move, in
the mover's transaction. The move to ``leased`` (the claim) gives the run its identities still free and live
(``browser_identities.held_by_run``, with ``last_used_at``), and any move out of ``runs.HELD_STATES`` (the run ends,
waits in review, is parked or lost, the reaper's moves included) takes back every identity the run holds, so the
next run of one may be claimed. An identity deleted meanwhile is left as it is: the lease of its profile refuses it.
"""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables


async def follow(conn: AsyncConnection, run_id: int, new: str) -> None:
    """Give run ``run_id`` its identities as it is claimed (``new`` is ``leased``), or take back those it holds as it
    leaves the held states; any other move holds what it held."""
    b = tables.browser_identities
    if new not in runs.HELD_STATES:
        await conn.execute(update(b).values(held_by_run=None).where(b.c.held_by_run == run_id))
        return
    if new != "leased":
        return
    r = tables.runs
    ids = (await conn.execute(select(r.c.browser_identities).where(r.c.id == run_id))).scalar_one_or_none()
    if not ids:
        return
    free = (b.c.id.in_(ids), b.c.held_by_run.is_(None), b.c.deleted_at.is_(None))
    await conn.execute(update(b).values(held_by_run=run_id, last_used_at=func.now()).where(*free))
