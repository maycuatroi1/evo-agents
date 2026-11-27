"""What the web's autonomy pages read (plan autonomy-levels, step 7), under /v1/autonomy:

- GET /v1/autonomy/settings: the caller's own autonomy for the page /settings/autonomy: the effective level and the
  mode of each class with its source, their preference, the ceiling the other sources set over it (the options above it
  show dimmed with its source), the stop that holds them, and for each class the hub records the figures of the last
  30 days: the acts refused and held for an answer (the ledger ``autonomy.decided``), the decisions asked in its
  categories, and for K5 and K6 the merges of their runs, the reverted ones and those PROD was rolled back from.
- GET /v1/autonomy/org: every seat's effective mode of each class, as its ceiling, its units' and the instance's set
  them, and the stops that hold; the org chart is everyone's to read, so is this.

The preview of a stop is GET /v1/autonomy/stops/preview (``autonomy.routes.stops``). A run's agent reads none of these.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.autonomy.models.page import AutonomySettings, OrgAutonomy
from evo_agents.hub.server.autonomy.service.page import read_org_autonomy, read_settings
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/autonomy", tags=["autonomy"], responses={401: {"model": ErrorBody}})


@router.get("/settings", response_model=AutonomySettings, responses={403: {"model": ErrorBody}})
async def show_settings(request: Request, user: CurrentUser) -> AutonomySettings:
    """The caller's own autonomy, preference, ceiling, stop and 30-day figures, for the web's settings page."""
    return await read_settings(request.app.state.engine, user)


@router.get("/org", response_model=OrgAutonomy, responses={403: {"model": ErrorBody}})
async def show_org_autonomy(request: Request, user: CurrentUser) -> OrgAutonomy:
    """Every seat's effective mode of each class, and the stops that hold, for the web's org page."""
    return await read_org_autonomy(request.app.state.engine, user)
