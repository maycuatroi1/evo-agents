"""POST /v1/worker/doctor: a worker's daemon sends its doctor report with its worker token (``service.doctor``)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.server.workers.models.doctor import DoctorAnswer, DoctorReport
from evo_agents.hub.server.workers.service import doctor

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


@worker_router.post("/doctor", response_model=DoctorAnswer, responses={403: {"model": ErrorBody}})
async def report_doctor(request: Request, report: DoctorReport, user: CurrentUser) -> DoctorAnswer:
    """Keep the newest doctor report of the worker whose token made the request; notify its owner when a report with a
    high finding stops it taking the runs of the units it is shared with."""
    return await doctor.keep(request.app.state.engine, user, report)
