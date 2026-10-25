"""The route of the plugin pm, under /v1/x/pm/<project>/ for a member with a grant on a project that enabled it (the
hub checks both first, ``evo_agents.hub.server.plugin_routes``; a project that did not enable it answers 404).

``GET /{project}/daily?date=YYYY-MM-DD`` answers the daily report of the project (``models.PmDaily``): the day is
today in the project's time zone when ``date`` is left out. A reader reads: the report shows the tasks, plans, runs and
decisions the routes of tasks show the caller (through the sink X-Evo-Sink names, else the project's hub sink), and
what is hidden adds to the counts of ``hidden`` only. Nothing is written.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Path, Query, Request
from fastapi.responses import JSONResponse

from evo_agents.core_plugins.pm import cli_text, reads, service
from evo_agents.core_plugins.pm.models import PmDaily
from evo_agents.core_plugins.pm.settings import PmError
from evo_agents.hub.server import core_plugin_api as hub

REFUSALS = {code: {"model": hub.ErrorBody} for code in (403, 404, 409, 422)}
ProjectName = Annotated[str, Path(pattern=hub.PROJECT_NAME)]
Sink = Annotated[str | None, Header(alias=reads.SINK_HEADER)]
Render = Annotated[
    Literal["text"] | None,
    Query(description="text: the words `evo-agents hub pm daily` prints, as {text}, instead of the report"),
]

router = APIRouter(tags=["plugins"], responses={401: {"model": hub.ErrorBody}, **REFUSALS})


@router.get("/{project}/daily", response_model=PmDaily)
async def daily(
    request: Request,
    project: ProjectName,
    user: hub.Caller,
    day: Annotated[date | None, Query(alias="date", description="the day of the report; today when left out")] = None,
    sink: Sink = None,
    render: Render = None,
):
    """The daily report: what was done on the working day before ``date``, the sprint, what is late, in review,
    stale or blocked, what to escalate, and the load of each member."""
    try:
        async with hub.begin(request) as conn:
            access = await hub.project_access(conn, user, project)
            report = await service.daily(conn, access, sink, day)
    except PmError as exc:
        raise hub.Refused(exc.status, exc.message) from None
    if render == "text":
        return JSONResponse({"text": cli_text.daily(report.model_dump(mode="json"))})
    return report


__all__ = ["router"]
