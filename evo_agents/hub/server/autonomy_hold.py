"""What a stop of autonomy S4 holds (plan autonomy-levels, criterion a11; ``autonomy_stops`` writes the stops): while a
stop holds, the hub queues no run whose principal it covers, a person's own (login), anyone with a live seat in a unit
or a unit below it (unit), or anyone at all, a run without a principal included (all). The refusal is ``Stopped``, a 403
that names the stop, raised where a run is inserted (``runs.service.dispatch.insert_run``, which every dispatch, the
Loop, the Curator's night shift and a release go through, and a chat run that goes on in a session).
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server import org_store

LOGIN, UNIT, ALL = "login", "unit", "all"
SCOPES = (LOGIN, UNIT, ALL)


class Stopped(HTTPException):
    """403: a stop of autonomy holds the run's principal."""

    def __init__(self, detail: str) -> None:
        super().__init__(403, detail)


def scope_name(scope: str, login: str | None, slug: str | None) -> str:
    """What a stop covers, as the hub names it: ``login:<login>``, ``unit:<slug>`` or ``all``."""
    if scope == LOGIN:
        return f"login:{login}"
    return f"unit:{slug}" if scope == UNIT else ALL


def live_stops():
    """The stops that hold, oldest first, with the login and the unit slug they name."""
    s, u, units = tables.autonomy_stops, tables.users, tables.org_units
    return (
        select(s.c.id, s.c.scope, s.c.user_id, s.c.unit_id, s.c.reason, s.c.stopped_at, u.c.login, units.c.slug)
        .select_from(s.outerjoin(u, u.c.id == s.c.user_id).outerjoin(units, units.c.id == s.c.unit_id))
        .where(s.c.lifted_at.is_(None))
        .order_by(s.c.id)
    )


async def held_by(conn: AsyncConnection, principal_id: int | None):
    """The oldest stop that holds the principal ``principal_id`` (None: a run without one, which a stop of all alone
    holds); None when no stop does."""
    stops = (await conn.execute(live_stops())).all()
    if not stops:
        return None
    for stop in stops:
        if stop.scope == ALL or (stop.scope == LOGIN and stop.user_id == principal_id):
            return stop
    units = [stop for stop in stops if stop.scope == UNIT]
    if principal_id is None or not units:
        return None
    chart, day = await org_store.load(conn), await org_store.today(conn)
    seats = [seat for seat in chart.seats_of_user(principal_id) if not chart.ended(seat, day)]
    for stop in units:
        if any(chart.within(seat.unit_id, stop.unit_id) for seat in seats):
            return stop
    return None


def refusal(stop) -> str:
    """The 403's sentence for a run that ``stop`` holds."""
    named = scope_name(stop.scope, stop.login, stop.slug)
    why = f" ({stop.reason})" if stop.reason else ""
    lift = f"--login {stop.login}" if stop.scope == LOGIN else f"--unit {stop.slug}" if stop.scope == UNIT else "--all"
    return (
        f"stop #{stop.id} of {named}{why}, since {stop.stopped_at.isoformat(timespec='seconds')}, holds every new run "
        f"of the principals it covers; nothing was queued. It holds until it is lifted: "
        f"`evo-agents hub stop {lift} --lift`"
    )


async def hold(conn: AsyncConnection, principal_id: int | None) -> None:
    """Stopped when a stop holds the principal ``principal_id``."""
    stop = await held_by(conn, principal_id)
    if stop is not None:
        raise Stopped(refusal(stop))


async def hold_run(conn: AsyncConnection, run_id: int) -> None:
    """Stopped when a stop holds the principal of run ``run_id``, whose session a new run goes on in."""
    r = tables.runs
    principal_id = (await conn.execute(select(r.c.principal_id).where(r.c.id == run_id))).scalar_one_or_none()
    await hold(conn, principal_id)
