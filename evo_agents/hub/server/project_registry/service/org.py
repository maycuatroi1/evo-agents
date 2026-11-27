"""The org chart's routes (``evo_agents.hub.server.org``), each request in one transaction: the chart and the runs at
work as a member sees them, and the writes of ``evo_agents.hub.server.org_writes`` with the leases they take back
(``evo_agents.hub.server.org_expiry``), which the route revokes once the transaction has committed."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import org
from evo_agents.hub.server import org_expiry, org_writes
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.org_models import (
    CeilingSet,
    CeilingWritten,
    OrgChartView,
    OrgRunCount,
    OrgRuns,
    OrgSeatView,
    OrgUnitView,
    SeatAdd,
    SeatEdit,
    UnitCreate,
    UnitEdit,
)
from evo_agents.hub.server.org_store import chart_view, load, names, seat_view, today, unit_view
from evo_agents.hub.server.runs.service.overview import running_by_member
from evo_agents.hub.server.security import Principal


def actor(user: Principal) -> org.Actor:
    """Who writes, as the rules of the chart see them; the agent of a run is refused."""
    if user.acts_for_run is not None:
        raise HTTPException(403, "the agent of a run never changes the org chart")
    return org.Actor(user_id=user.user_id, login=user.login, admin=user.admin)


async def begin(conn: AsyncConnection, user: Principal) -> org_writes.Write:
    return await org_writes.begin(conn, actor(user), user.token_id)


async def unit(conn: AsyncConnection, slug: str) -> OrgUnitView:
    chart = await load(conn)
    found = chart.unit_by_slug(slug)
    assert found is not None
    return unit_view(chart, found)


async def seat(conn: AsyncConnection, seat_id: int) -> OrgSeatView:
    chart = await load(conn)
    return seat_view(chart, chart.seats[seat_id], await names(conn, chart), await today(conn))


async def take_back(conn: AsyncConnection, user: Principal) -> list[int]:
    """After a write that may end a person's primary seat (an end, an end date moved): the leases of the people at A0
    given back in its transaction (``org_expiry.take_back``)."""
    return await org_expiry.take_back_now(conn, actor_id=user.user_id, token_id=user.token_id, by=org_expiry.BY_WRITE)


async def show(engine: AsyncEngine, user: Principal) -> OrgChartView:
    """The whole org chart, with every ceiling and effective level, and the viewer's own."""
    async with UnitOfWork(engine) as conn:
        return await chart_view(conn, org.Actor(user_id=user.user_id, login=user.login, admin=user.admin))


async def runs(engine: AsyncEngine, user: Principal) -> OrgRuns:
    """Each member's runs at work now, by project, over the projects the viewer holds a grant on."""
    async with UnitOfWork(engine) as conn:
        counted = await running_by_member(conn, user)
    return OrgRuns(running=[OrgRunCount(login=login, project=project, running=n) for login, project, n in counted])


async def create_unit(engine: AsyncEngine, user: Principal, body: UnitCreate) -> OrgUnitView:
    async with UnitOfWork(engine) as conn:
        slug = await org_writes.create_unit(await begin(conn, user), body)
        return await unit(conn, slug)


async def edit_unit(engine: AsyncEngine, user: Principal, slug: str, body: UnitEdit) -> tuple[OrgUnitView, list[int]]:
    """The unit edited, and the runs whose leases were taken back, for the route to revoke."""
    async with UnitOfWork(engine) as conn:
        await org_writes.edit_unit(await begin(conn, user), slug, body)
        taken = await take_back(conn, user)
        view = await unit(conn, slug)
    return view, taken


async def archive_unit(engine: AsyncEngine, user: Principal, slug: str) -> OrgUnitView:
    async with UnitOfWork(engine) as conn:
        await org_writes.archive_unit(await begin(conn, user), slug)
        return await unit(conn, slug)


async def add_seat(engine: AsyncEngine, user: Principal, body: SeatAdd) -> OrgSeatView:
    async with UnitOfWork(engine) as conn:
        seat_id = await org_writes.add_seat(await begin(conn, user), body)
        return await seat(conn, seat_id)


async def edit_seat(
    engine: AsyncEngine, user: Principal, seat_id: int, body: SeatEdit
) -> tuple[OrgSeatView, list[int]]:
    """The seat edited, and the runs whose leases were taken back, for the route to revoke."""
    async with UnitOfWork(engine) as conn:
        await org_writes.edit_seat(await begin(conn, user), seat_id, body)
        taken = await take_back(conn, user)
        view = await seat(conn, seat_id)
    return view, taken


async def end_seat(engine: AsyncEngine, user: Principal, seat_id: int) -> tuple[OrgSeatView, list[int]]:
    """The seat ended, and the runs whose leases were taken back, for the route to revoke."""
    async with UnitOfWork(engine) as conn:
        await org_writes.end_seat(await begin(conn, user), seat_id)
        taken = await take_back(conn, user)
        view = await seat(conn, seat_id)
    return view, taken


async def set_ceiling(engine: AsyncEngine, user: Principal, body: CeilingSet) -> CeilingWritten:
    async with UnitOfWork(engine) as conn:
        return await org_writes.set_ceiling(await begin(conn, user), body)
