"""Run stats by day, for the web's charts: GET /v1/projects/{p}/runs/stats (``service.stats``)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Request

from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import MAX_STATS_DAYS, MIN_STATS_DAYS, STATS_DAYS, RunStats
from evo_agents.hub.server.runs.service.stats import daily_stats
from evo_agents.hub.server.security import CurrentUser

router = APIRouter()


@router.get(
    "/{project}/runs/stats",
    response_model=RunStats,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def run_stats(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    days: Annotated[
        int,
        Query(ge=MIN_STATS_DAYS, le=MAX_STATS_DAYS, description="the last days in UTC to count, today included"),
    ] = STATS_DAYS,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> RunStats:
    """The runs of the plans the caller may read that ended on each of the last ``days`` days in UTC: how many in
    each end state, how long they ran and the tokens they used."""
    return await daily_stats(request.app.state.engine, user, project, days, sink)
