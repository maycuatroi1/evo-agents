"""The stop of autonomy S4 (plan autonomy-levels, criterion a11), under /v1/autonomy/stops.

- POST /v1/autonomy/stops: stop the runs of a person (``login``), of everyone with a live seat in a unit or a unit
  below it (``unit``), or of the whole hub (``all``). A person stops their own, a lead those of the units they lead
  and below, a hub admin anyone and the hub (``org.stop_refusal``); the agent of a run never does. The stop cancels
  every run of the principals it covers that has not ended, as ``hub run cancel`` does: a queued or parked run, or one
  in review, at once; a held run is asked to stop, and its worker stops it at its next heartbeat. It takes back their
  leases (``lease_ending.end_leases``; the GitHub tokens are revoked once the stop is written). Until it is lifted,
  the hub queues no run of those principals, the runs it makes itself included (``autonomy.service.hold``). It writes
  the audit row ``autonomy.stop`` and tells each person whose runs it stopped (the notice ``autonomy``). A second stop
  of the same scope while one holds is refused (409).
- POST /v1/autonomy/stops/lift: lift the stop that holds for that scope, which the same people may do; it writes
  ``autonomy.stop.lift`` and tells the same people. No stop holding there is a 404.
- GET /v1/autonomy/stops/preview: what a stop of that scope would cancel now, for the web's confirmation; the same
  people may read it, and it writes nothing.

A local run is only recorded by the hub, which neither cancels it nor holds it.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Query, Request

from evo_agents.hub.server.autonomy.models.page import StopPreview
from evo_agents.hub.server.autonomy.models.stops import StopSet, StopView
from evo_agents.hub.server.autonomy.service import stops
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}

router = APIRouter(prefix="/v1/autonomy/stops", tags=["autonomy"], responses={401: {"model": ErrorBody}})


@router.post("", response_model=StopView, status_code=201, responses=REFUSALS)
async def stop_autonomy(request: Request, body: StopSet, user: CurrentUser) -> StopView:
    """Stop the runs of a person, a unit or the hub, and hold new ones until the stop is lifted."""
    async with request.app.state.engine.begin() as conn:
        view = await stops.stop(conn, user, body)
    await stops.revoke(request.app.state, view.runs)
    log.info("autonomy stopped", extra={"stop": view.id, "target": view.target, "runs": len(view.runs)})
    return view


@router.post("/lift", response_model=StopView, responses=REFUSALS)
async def lift_stop(request: Request, body: StopSet, user: CurrentUser) -> StopView:
    """Lift the stop that holds for a person, a unit or the hub."""
    async with request.app.state.engine.begin() as conn:
        view = await stops.lift(conn, user, body)
    log.info("autonomy stop lifted", extra={"stop": view.id, "target": view.target})
    return view


@router.get("/preview", response_model=StopPreview, responses=REFUSALS)
async def preview_stop(
    request: Request,
    user: CurrentUser,
    scope: Literal["login", "unit", "all"],
    login: str | None = Query(None, max_length=100),
    unit: str | None = Query(None, max_length=63),
) -> StopPreview:
    """How many runs and people a stop of that scope would cover now, and whether one holds already."""
    body = stops.stop_set(scope, login, unit)
    async with request.app.state.engine.connect() as conn:
        return await stops.preview(conn, user, body)
