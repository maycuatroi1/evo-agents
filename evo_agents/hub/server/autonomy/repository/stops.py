"""Every query of the stops of autonomy S4, table autonomy_stops, and of the runs and leases a stop sweeps."""

from __future__ import annotations

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables


async def live_stops(conn: AsyncConnection):
    """The stops that hold, oldest first, with the login and the unit slug they name."""
    s, u, units = tables.autonomy_stops, tables.users, tables.org_units
    query = (
        select(s.c.id, s.c.scope, s.c.user_id, s.c.unit_id, s.c.reason, s.c.stopped_at, u.c.login, units.c.slug)
        .select_from(s.outerjoin(u, u.c.id == s.c.user_id).outerjoin(units, units.c.id == s.c.unit_id))
        .where(s.c.lifted_at.is_(None))
        .order_by(s.c.id)
    )
    return (await conn.execute(query)).all()


async def principal_of(conn: AsyncConnection, run_id: int) -> int | None:
    """The principal of the run; None for a run without one, or no such run."""
    r = tables.runs
    return (await conn.execute(select(r.c.principal_id).where(r.c.id == run_id))).scalar_one_or_none()


def _open(query, covered: set[int] | None):
    """``query`` kept to the runs of ``covered`` (None: every principal) that have not ended, a local run aside."""
    r = tables.runs
    query = query.where(r.c.state.not_in(runs.TERMINAL_STATES), r.c.kind != "local")
    return query if covered is None else query.where(r.c.principal_id.in_(covered))


async def open_runs(conn: AsyncConnection, covered: set[int] | None):
    """The runs of ``covered`` (None: every principal) that have not ended, a local run aside, locked, oldest first:
    (id, state, cancel_requested_at, principal_id)."""
    r = tables.runs
    query = _open(select(r.c.id, r.c.state, r.c.cancel_requested_at, r.c.principal_id), covered)
    return (await conn.execute(query.order_by(r.c.id).with_for_update(of=r))).all()


async def count_open_runs(conn: AsyncConnection, covered: set[int] | None) -> int:
    """How many runs ``open_runs`` would read."""
    return (await conn.execute(_open(select(func.count()), covered))).scalar_one()


async def count_open_leases(conn: AsyncConnection, run_ids: list[int]) -> int:
    """How many leases of the runs are not revoked."""
    cl = tables.credential_leases
    query = select(func.count()).where(cl.c.run_id.in_(run_ids), cl.c.revoked_at.is_(None))
    return (await conn.execute(query)).scalar_one()


async def ask_cancel(conn: AsyncConnection, run_id: int) -> None:
    """Ask the run to stop: its worker stops it at its next heartbeat."""
    r = tables.runs
    await conn.execute(update(r).values(cancel_requested_at=func.now()).where(r.c.id == run_id))


async def logins(conn: AsyncConnection, people: set[int]) -> list[str]:
    """The logins of ``people``, sorted."""
    u = tables.users
    return sorted((await conn.execute(select(u.c.login).where(u.c.id.in_(people)))).scalars())


async def live_id(conn: AsyncConnection, scope: str, user_id: int | None, unit_id: int | None) -> int | None:
    """The id of the oldest stop of ``scope`` that holds for the person or the unit; None when none does."""
    s = tables.autonomy_stops
    query = select(s.c.id).where(s.c.scope == scope, s.c.lifted_at.is_(None))
    if user_id is not None:
        query = query.where(s.c.user_id == user_id)
    if unit_id is not None:
        query = query.where(s.c.unit_id == unit_id)
    return (await conn.execute(query.order_by(s.c.id).limit(1))).scalar_one_or_none()


async def add(conn: AsyncConnection, values: dict) -> int:
    """Insert the stop ``values`` names; its id."""
    s = tables.autonomy_stops
    return (await conn.execute(insert(s).values(**values).returning(s.c.id))).scalar_one()


async def mark_lifted(conn: AsyncConnection, stop_id: int, user_id: int) -> None:
    s = tables.autonomy_stops
    await conn.execute(update(s).values(lifted_by=user_id, lifted_at=func.now()).where(s.c.id == stop_id))


async def stop_row(conn: AsyncConnection, stop_id: int):
    """The stop with the logins of who stopped and lifted it, the slug of its unit and the login it names."""
    s, by, lifter, units, named = (
        tables.autonomy_stops,
        tables.users.alias("by"),
        tables.users.alias("lifter"),
        tables.org_units,
        tables.users.alias("named"),
    )
    query = (
        select(s, by.c.login.label("by_login"), lifter.c.login.label("lifter_login"), units.c.slug, named.c.login)
        .select_from(
            s.join(by, by.c.id == s.c.stopped_by)
            .outerjoin(lifter, lifter.c.id == s.c.lifted_by)
            .outerjoin(units, units.c.id == s.c.unit_id)
            .outerjoin(named, named.c.id == s.c.user_id)
        )
        .where(s.c.id == stop_id)
    )
    return (await conn.execute(query)).one()
