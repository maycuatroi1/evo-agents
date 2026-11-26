"""The route of the weekly report of the Curator's probes: GET /v1/projects/{p}/curator/probe-report (reader), for
``weeks`` weeks, the newest first, the week of ``week`` and those before it (``service.report.build``)."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request

from evo_agents.hub import probes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.probes.models.report import DEFAULT_WEEKS, ProbeWeekly
from evo_agents.hub.server.probes.service.report import build
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.protection import need_reader
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
READ_REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404)}

Weeks = Annotated[
    int, Query(ge=1, le=probes.MAX_REPORT_WEEKS, description="the newest weeks, the one in progress first")
]


@router.get("/{project}/curator/probe-report", response_model=ProbeWeekly, responses=READ_REFUSALS)
async def probe_report(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    weeks: Weeks = DEFAULT_WEEKS,
    week: Annotated[
        date | None, Query(description="a day of the newest week to show; default today, in the charter's time zone")
    ] = None,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> ProbeWeekly:
    """The weekly report of the project's probes."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        return await build(conn, access, user, weeks=weeks, newest=week, sink=sink)
