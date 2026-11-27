"""The route of a probe run's results under /v1/worker: POST /v1/worker/runs/{id}/probe takes one session's result of
the probe run the worker holds, or why the probe is stale, with the run's own key (``probes.PROBE_KEY_HEADER``) alone
(``service.runs.report``)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Request

from evo_agents.hub import probes
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.probes.models.runs import ProbeReport, ProbeRun
from evo_agents.hub.server.probes.service import runs as service
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.security import CurrentUser

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409)}

ProbeKey = Annotated[
    str | None,
    Header(
        alias=probes.PROBE_KEY_HEADER,
        max_length=200,
        description="the probe run's own key, which its claim handed the daemon; the worker token alone is refused",
    ),
]


@worker_router.post("/runs/{run_id}/probe", response_model=ProbeRun, responses=REFUSALS)
async def report_probe(
    request: Request, run_id: RunId, body: ProbeReport, user: CurrentUser, probe_key: ProbeKey = None
) -> ProbeRun:
    """One session's result of the probe run this worker holds, or why the probe is stale; with the run's key."""
    return await service.report(request.app.state.engine, user, run_id, body, probe_key)
