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

from fastapi import APIRouter, Path, Request

from evo_agents.hub import org
from evo_agents.hub.server import org_expiry
from evo_agents.hub.server.autonomy.service import guards as route_autonomy
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.lease_ending import revoker
from evo_agents.hub.server.org_models import (
    CeilingSet,
    CeilingWritten,
    OrgChartView,
    OrgRuns,
    OrgSeatView,
    OrgUnitView,
    SeatAdd,
    SeatEdit,
    UnitCreate,
    UnitEdit,
)
from evo_agents.hub.server.project_registry.service import org as service
from evo_agents.hub.server.project_registry.service.org import actor as actor
from evo_agents.hub.server.security import CurrentUser

MAX_SEAT_ID = 2**63 - 1
WRITE_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
UnitSlug = Annotated[str, Path(pattern=org.SLUG_PATTERN)]
SeatId = Annotated[int, Path(ge=1, le=MAX_SEAT_ID)]

router = APIRouter(
    prefix="/v1/org", tags=["org"], dependencies=[route_autonomy.K12], responses={401: {"model": ErrorBody}}
)


async def _revoke(request: Request, run_ids: list[int]) -> None:
    state = request.app.state
    await org_expiry.revoke_taken(state.engine, state.sealer, revoker(state), run_ids)


@router.get("", response_model=OrgChartView)
async def show_org(request: Request, user: CurrentUser) -> OrgChartView:
    """The whole org chart, with every ceiling and effective level, and the viewer's own."""
    return await service.show(request.app.state.engine, user)


@router.get("/runs", response_model=OrgRuns)
async def org_runs(request: Request, user: CurrentUser) -> OrgRuns:
    """Each member's runs at work now, by project, over the projects the viewer holds a grant on."""
    return await service.runs(request.app.state.engine, user)


@router.post("/units", status_code=201, response_model=OrgUnitView, responses=WRITE_REFUSALS)
async def create_unit(request: Request, body: UnitCreate, user: CurrentUser) -> OrgUnitView:
    """Create a team or a cohort below a unit; a cohort without a level gets a ceiling of A1."""
    return await service.create_unit(request.app.state.engine, user, body)


@router.patch("/units/{slug}", response_model=OrgUnitView, responses=WRITE_REFUSALS)
async def edit_unit(request: Request, slug: UnitSlug, body: UnitEdit, user: CurrentUser) -> OrgUnitView:
    """Change a unit's name, parent or end date; a field left out keeps its value."""
    view, taken = await service.edit_unit(request.app.state.engine, user, slug, body)
    await _revoke(request, taken)
    return view


@router.post("/units/{slug}/archive", response_model=OrgUnitView, responses=WRITE_REFUSALS)
async def archive_unit(request: Request, slug: UnitSlug, user: CurrentUser) -> OrgUnitView:
    """Archive a unit with no unit below it left and no live seat."""
    return await service.archive_unit(request.app.state.engine, user, slug)


@router.post("/seats", status_code=201, response_model=OrgSeatView, responses=WRITE_REFUSALS)
async def add_seat(request: Request, body: SeatAdd, user: CurrentUser) -> OrgSeatView:
    """Add a seat of a person or an agent profile to a unit."""
    return await service.add_seat(request.app.state.engine, user, body)


@router.patch("/seats/{seat_id}", response_model=OrgSeatView, responses=WRITE_REFUSALS)
async def edit_seat(request: Request, seat_id: SeatId, body: SeatEdit, user: CurrentUser) -> OrgSeatView:
    """Change a live seat's role, reports_to, sponsor or dates, or make it the person's primary seat."""
    view, taken = await service.edit_seat(request.app.state.engine, user, seat_id, body)
    await _revoke(request, taken)
    return view


@router.post("/seats/{seat_id}/end", response_model=OrgSeatView, responses=WRITE_REFUSALS)
async def end_seat(request: Request, seat_id: SeatId, user: CurrentUser) -> OrgSeatView:
    """End a seat now."""
    view, taken = await service.end_seat(request.app.state.engine, user, seat_id)
    await _revoke(request, taken)
    return view


@router.put("/ceilings", response_model=CeilingWritten, responses=WRITE_REFUSALS)
async def set_ceiling(request: Request, body: CeilingSet, user: CurrentUser) -> CeilingWritten:
    """Write the next ceiling revision of a unit, of a person's primary seat or of a seat."""
    return await service.set_ceiling(request.app.state.engine, user, body)
