"""Which run holds a browser identity: the one a worker holds, from its claim until it leaves the held states.

A run dispatched with identities (``runs.browser_identities``) is claimed only while none of them is held
(``runs.service.browsers.claim_gate``); ``follow`` is what ``run_state.move_run`` does with them on every move, in
the mover's transaction. The move to ``leased`` (the claim) gives the run its identities still free and live
(``browser_identities.held_by_run``, with ``last_used_at``), and any move out of ``runs.HELD_STATES`` (the run ends,
waits in review, is parked or lost, the reaper's moves included) takes back every identity the run holds, so the
next run of one may be claimed. An identity deleted meanwhile is left as it is: the lease of its profile refuses it.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs
from evo_agents.hub.server.browser.repository import holds as repository


async def follow(conn: AsyncConnection, run_id: int, new: str) -> None:
    """Give run ``run_id`` its identities as it is claimed (``new`` is ``leased``), or take back those it holds as it
    leaves the held states; any other move holds what it held."""
    if new not in runs.HELD_STATES:
        await repository.release(conn, run_id)
        return
    if new == "leased":
        await take(conn, run_id)


async def take(conn: AsyncConnection, run_id: int) -> None:
    """Give run ``run_id`` the identities it was dispatched with that are still free and live: at its claim, and when
    the hub re-attaches it after its loss gave them back (``run_reattach``)."""
    ids = await repository.dispatched_with(conn, run_id)
    if not ids:
        return
    await repository.hold(conn, run_id, ids)
