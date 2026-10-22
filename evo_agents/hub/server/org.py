"""The routes of the org chart (plan org-chart, criteria a3 and a4), under /v1/org.

- GET /v1/org: the whole chart for any member signed in: every unit and seat with its ceiling, its effective level and
  the source of it, and the viewer's own (``evo_agents.hub.server.org_store``).
- GET /v1/org/runs: each member's runs at work now, by project, counted over the projects the viewer holds a grant on
  and as Home counts them (``evo_agents.hub.server.overview.running_by_member``), for the seats of the web's /org.
- POST /v1/org/units, PATCH /v1/org/units/{slug}, POST /v1/org/units/{slug}/archive: create, edit and archive a unit.
- POST /v1/org/seats, PATCH /v1/org/seats/{seat_id}, POST /v1/org/seats/{seat_id}/end: add, edit and end a seat.
- PUT /v1/org/ceilings: the next ceiling revision of a unit, of a person's primary seat or of a seat.

Who may write what is ``evo_agents.hub.org`` (403 naming why); the writes are ``evo_agents.hub.server.org_writes``,
each one transaction under the chart's lock with its audit row. The agent of a run never writes the chart. Ending a
seat, editing one or editing a unit takes back the leases of the people whose primary seat that ended
(``evo_agents.hub.server.org_expiry``), as the hourly job does for the seats past their end date.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import org
from evo_agents.hub.server import org_expiry, org_writes, overview
from evo_agents.hub.server.credentials import revoker
from evo_agents.hub.server.errors import ErrorBody
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
from evo_agents.hub.server.security import CurrentUser, Principal

MAX_SEAT_ID = 2**63 - 1
WRITE_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
UnitSlug = Annotated[str, Path(pattern=org.SLUG_PATTERN)]
SeatId = Annotated[int, Path(ge=1, le=MAX_SEAT_ID)]

router = APIRouter(prefix="/v1/org", tags=["org"], responses={401: {"model": ErrorBody}})


def actor(user: Principal) -> org.Actor:
    """Who writes, as the rules of the chart see them; the agent of a run is refused."""
    if user.scope is not None:
        raise HTTPException(403, "the agent of a run never changes the org chart")
    return org.Actor(user_id=user.user_id, login=user.login, admin=user.admin)


async def _begin(conn: AsyncConnection, user: Principal) -> org_writes.Write:
    return await org_writes.begin(conn, actor(user), user.token_id)


async def _unit(conn: AsyncConnection, slug: str) -> OrgUnitView:
    chart = await load(conn)
    found = chart.unit_by_slug(slug)
    assert found is not None
    return unit_view(chart, found)


async def _seat(conn: AsyncConnection, seat_id: int) -> OrgSeatView:
    chart = await load(conn)
    return seat_view(chart, chart.seats[seat_id], await names(conn, chart), await today(conn))


async def _take_back(conn: AsyncConnection, user: Principal) -> list[int]:
    """After a write that may end a person's primary seat (an end, an end date moved): the leases of the people at A0
    given back in its transaction (``org_expiry.take_back``)."""
    return await org_expiry.take_back_now(conn, actor_id=user.user_id, token_id=user.token_id, by=org_expiry.BY_WRITE)


async def _revoke(request: Request, run_ids: list[int]) -> None:
    state = request.app.state
    await org_expiry.revoke_taken(state.engine, state.sealer, revoker(state), run_ids)


@router.get("", response_model=OrgChartView)
async def show_org(request: Request, user: CurrentUser) -> OrgChartView:
    """The whole org chart, with every ceiling and effective level, and the viewer's own."""
    async with request.app.state.engine.begin() as conn:
        return await chart_view(conn, org.Actor(user_id=user.user_id, login=user.login, admin=user.admin))


@router.get("/runs", response_model=OrgRuns)
async def org_runs(request: Request, user: CurrentUser) -> OrgRuns:
    """Each member's runs at work now, by project, over the projects the viewer holds a grant on."""
    async with request.app.state.engine.begin() as conn:
        counted = await overview.running_by_member(conn, user)
    return OrgRuns(running=[OrgRunCount(login=login, project=project, running=n) for login, project, n in counted])


@router.post("/units", status_code=201, response_model=OrgUnitView, responses=WRITE_REFUSALS)
async def create_unit(request: Request, body: UnitCreate, user: CurrentUser) -> OrgUnitView:
    """Create a team or a cohort below a unit; a cohort without a level gets a ceiling of A1."""
    async with request.app.state.engine.begin() as conn:
        slug = await org_writes.create_unit(await _begin(conn, user), body)
        return await _unit(conn, slug)


@router.patch("/units/{slug}", response_model=OrgUnitView, responses=WRITE_REFUSALS)
async def edit_unit(request: Request, slug: UnitSlug, body: UnitEdit, user: CurrentUser) -> OrgUnitView:
    """Change a unit's name, parent or end date; a field left out keeps its value."""
    async with request.app.state.engine.begin() as conn:
        await org_writes.edit_unit(await _begin(conn, user), slug, body)
        taken = await _take_back(conn, user)
        view = await _unit(conn, slug)
    await _revoke(request, taken)
    return view


@router.post("/units/{slug}/archive", response_model=OrgUnitView, responses=WRITE_REFUSALS)
async def archive_unit(request: Request, slug: UnitSlug, user: CurrentUser) -> OrgUnitView:
    """Archive a unit with no unit below it left and no live seat."""
    async with request.app.state.engine.begin() as conn:
        await org_writes.archive_unit(await _begin(conn, user), slug)
        return await _unit(conn, slug)


@router.post("/seats", status_code=201, response_model=OrgSeatView, responses=WRITE_REFUSALS)
async def add_seat(request: Request, body: SeatAdd, user: CurrentUser) -> OrgSeatView:
    """Add a seat of a person or an agent profile to a unit."""
    async with request.app.state.engine.begin() as conn:
        seat_id = await org_writes.add_seat(await _begin(conn, user), body)
        return await _seat(conn, seat_id)


@router.patch("/seats/{seat_id}", response_model=OrgSeatView, responses=WRITE_REFUSALS)
async def edit_seat(request: Request, seat_id: SeatId, body: SeatEdit, user: CurrentUser) -> OrgSeatView:
    """Change a live seat's role, reports_to, sponsor or dates, or make it the person's primary seat."""
    async with request.app.state.engine.begin() as conn:
        await org_writes.edit_seat(await _begin(conn, user), seat_id, body)
        taken = await _take_back(conn, user)
        view = await _seat(conn, seat_id)
    await _revoke(request, taken)
    return view


@router.post("/seats/{seat_id}/end", response_model=OrgSeatView, responses=WRITE_REFUSALS)
async def end_seat(request: Request, seat_id: SeatId, user: CurrentUser) -> OrgSeatView:
    """End a seat now."""
    async with request.app.state.engine.begin() as conn:
        await org_writes.end_seat(await _begin(conn, user), seat_id)
        taken = await _take_back(conn, user)
        view = await _seat(conn, seat_id)
    await _revoke(request, taken)
    return view


@router.put("/ceilings", response_model=CeilingWritten, responses=WRITE_REFUSALS)
async def set_ceiling(request: Request, body: CeilingSet, user: CurrentUser) -> CeilingWritten:
    """Write the next ceiling revision of a unit, of a person's primary seat or of a seat."""
    async with request.app.state.engine.begin() as conn:
        return await org_writes.set_ceiling(await _begin(conn, user), body)
