"""Seats that end (plan org-chart, criterion a5): the hourly job that ends the seats past their end date, and the leases
the hub takes back from the people whose primary seat ended.

``expire_seats`` (the job hub.expire_seats, ``evo_agents.hub.worker``) ends, under the chart's lock, every live seat
whose ends_on or that of a cohort holding it passed: ended_at now, the unit's revision raised, and one audit row
org.seat_end with no actor (the hub) for each. A seat it ended has its ended_at, so running it again writes nothing.

A person whose primary seat ended is at A0 (``org.ended_primary``). The hub takes back the leases still out of every run
they dispatched, on every project, as it does when a grant falls below writer (``credentials.end_leases``, one audit
row credential.revoke per run), and their GitHub tokens are revoked at GitHub once the transaction commits
(``revoke_taken``). ``take_back`` does it for the job and for the routes that end a seat or move an end date
(``evo_agents.hub.server.org``). The worker of a run they dispatched then gets 403 when it asks for credentials, and so
does every new run they would dispatch or ask of an agent in a chat (``org_store.ended_refusal``). What their grants
let them read stays.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import org, tables
from evo_agents.hub.server import audit, credentials
from evo_agents.hub.server.org_store import load, lock, today

log = logging.getLogger(__name__)

BY_HUB = "org-expiry"  # the by= of the credential.revoke rows of the job
BY_WRITE = "org-seat-end"  # those of a write of the chart that ended a person's primary seat


async def _leased_runs(conn: AsyncConnection, user_ids: list[int]) -> list[int]:
    """The runs dispatched by ``user_ids``, on any project, that still hold a lease not given back."""
    if not user_ids:
        return []
    cl, r = tables.credential_leases, tables.runs
    leased = (
        select(cl.c.run_id)
        .distinct()
        .join_from(cl, r, r.c.id == cl.c.run_id)
        .where(cl.c.revoked_at.is_(None), r.c.dispatched_by.in_(user_ids))
        .order_by(cl.c.run_id)
    )
    return list((await conn.execute(leased)).scalars())


async def take_back(
    conn: AsyncConnection, chart: org.Chart, *, actor_id: int | None, token_id: int | None, by: str
) -> list[int]:
    """Give back, in the caller's transaction, every lease still out of the runs dispatched by the people whose primary
    seat ended in ``chart``; the runs, whose GitHub tokens ``revoke_taken`` revokes once the transaction commits."""
    run_ids = await _leased_runs(conn, org.out_of_office(chart, await today(conn)))
    for run_id in run_ids:
        await credentials.end_leases(conn, run_id=run_id, actor_id=actor_id, token_id=token_id, by=by)
    return run_ids


async def take_back_now(conn: AsyncConnection, *, actor_id: int | None, token_id: int | None, by: str) -> list[int]:
    """``take_back`` on the chart as the caller's transaction holds it now."""
    return await take_back(conn, await load(conn), actor_id=actor_id, token_id=token_id, by=by)


async def revoke_taken(engine: AsyncEngine, sealer, app, run_ids: list[int]) -> None:
    """Revoke at GitHub the tokens of the leases ``take_back`` gave back; GitHub failing leaves them to the reaper."""
    for run_id in run_ids:
        await credentials.revoke_tokens(engine, sealer, app, run_id=run_id)


async def _end(conn: AsyncConnection, chart: org.Chart, seat: org.Seat, day) -> None:
    s, u = tables.org_seats, tables.org_units
    await conn.execute(update(s).values(ended_at=func.now(), updated_at=func.now()).where(s.c.id == seat.id))
    raised = await conn.execute(
        update(u)
        .values(revision=u.c.revision + 1, updated_at=func.now())
        .where(u.c.id == seat.unit_id)
        .returning(u.c.revision)
    )
    unit = chart.units[seat.unit_id].slug
    target = f"seat:{seat.id} unit={unit} ended=none->now ends_on={day} by=hub revision={raised.scalar_one()}"
    await audit.record(conn, actor_id=None, token_id=None, action=audit.ORG_SEAT_END, target=target)


async def expire_seats(engine: AsyncEngine, sealer=None, app=None) -> dict:
    """End every live seat past its end date or that of a cohort holding it, and take back the leases of the people
    whose primary seat ended; how many seats ended and how many runs gave leases back."""
    async with engine.begin() as conn:
        await lock(conn)
        chart, day = await load(conn), await today(conn)
        ending = org.overdue(chart, day)
        for seat in ending:
            await _end(conn, chart, seat, org.end_day(chart, seat, day))
        run_ids = await take_back(conn, chart, actor_id=None, token_id=None, by=BY_HUB)
    await revoke_taken(engine, sealer, app, run_ids)
    report = {"ended": len(ending), "runs": len(run_ids)}
    if ending or run_ids:
        log.info("org seats expired", extra={**report, "seats": [seat.id for seat in ending]})
    return report
