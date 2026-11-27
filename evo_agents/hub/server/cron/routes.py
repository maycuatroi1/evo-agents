"""The cron jobs of a project (``docs/hub.md``, Cron jobs; plan hub-cron-jobs, criterion a1).

- GET /v1/projects/{p}/cron-jobs: its jobs, by name, each with its next fire in its time zone, its last fire and what
  the runs of its fires cost in the last 24 hours.
- GET .../cron-jobs/{name}: one.
- PUT .../cron-jobs/{name} {cron, timezone, kind, ..., if_revision}: creates it (no ``if_revision``, or 0), owned by
  the caller, or stores what it starts as its next revision (``if_revision`` its revision now; 409
  ``revision_conflict`` otherwise, with the job as the hub holds it). 422 with the reason for a cron that does not
  read, a time zone the hub does not know, a job of kind agent or plan that fires more often than every 15 minutes, a
  job of kind command more often than every 5 (``evo_agents.hub.cron``), or work that names what the project lacks.
- POST .../cron-jobs/{name}/pause {reason}, .../resume: a paused job does not fire.
- POST .../cron-jobs/{name}/run: fire it now, a fire due at the moment it was pressed, which the hub handles as any
  fire (outcome null until then).
- GET .../cron-jobs/{name}/fires[?limit=N]: its fires, the latest due first.
- DELETE .../cron-jobs/{name}: deletes it with its fires; its runs stay.

Every member of the project reads its jobs and their fires. A writer makes jobs, and changes, pauses, resumes, fires and
deletes their own; an admin of the project pauses, resumes, fires and deletes anyone's, but changes only their own:
what a job starts runs as its owner. Writes are audited as ``cron.*``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.cron.models import (
    REVISION_CONFLICT,
    CronFireOut,
    CronJobConflict,
    CronJobDeleted,
    CronJobOut,
    CronJobPause,
    CronJobPut,
    CronJobWritten,
)
from evo_agents.hub.server.cron.service import jobs as service
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.tables_cron import NAME_PATTERN

router = APIRouter(prefix="/v1/projects", tags=["cron-jobs"], responses={401: {"model": ErrorBody}})
Name = Annotated[str, Path(min_length=1, max_length=63, pattern=NAME_PATTERN, description="the job's name")]
WRITE_REFUSALS: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    409: {"model": CronJobConflict},
    422: {"model": ErrorBody},
}
REFUSALS: dict[int | str, dict[str, Any]] = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}


def _conflict(request: Request, exc: service.RevisionConflict) -> JSONResponse:
    body = CronJobConflict(
        error=REVISION_CONFLICT,
        message=exc.message,
        request_id=getattr(request.state, "request_id", None),
        current=exc.current,
    )
    return JSONResponse(body.model_dump(mode="json", exclude_none=True), status_code=409)


# Reading


@router.get("/{project}/cron-jobs", response_model=list[CronJobOut], responses=REFUSALS)
async def list_cron_jobs(request: Request, project: ProjectName, user: CurrentUser) -> list[CronJobOut]:
    """The cron jobs of the project, by name, with their next fire, last fire and cost in 24 hours."""
    async with request.app.state.engine.connect() as conn:
        return await service.listed(conn, user, project)


@router.get("/{project}/cron-jobs/{name}", response_model=CronJobOut, responses=REFUSALS)
async def show_cron_job(request: Request, project: ProjectName, name: Name, user: CurrentUser) -> CronJobOut:
    """One cron job."""
    async with request.app.state.engine.connect() as conn:
        return await service.one(conn, user, project, name)


@router.get("/{project}/cron-jobs/{name}/fires", response_model=list[CronFireOut], responses=REFUSALS)
async def cron_job_fires(
    request: Request,
    project: ProjectName,
    name: Name,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=500, description="at most this many fires")] = 50,
) -> list[CronFireOut]:
    """The fires of a cron job, the latest due first."""
    async with request.app.state.engine.connect() as conn:
        return await service.fires(conn, user, project, name, limit)


# Changing


@router.put("/{project}/cron-jobs/{name}", response_model=CronJobWritten, responses=WRITE_REFUSALS)
async def put_cron_job(request: Request, project: ProjectName, name: Name, body: CronJobPut, user: CurrentUser):
    """Create the cron job, owned by the caller, or store what it starts as its next revision."""
    try:
        written = await service.put(request.app.state.engine, user, project, name, body)
    except service.RevisionConflict as exc:
        return _conflict(request, exc)
    service.stored(project, name, written)
    return written


@router.post("/{project}/cron-jobs/{name}/pause", response_model=CronJobOut, responses=REFUSALS)
async def pause_cron_job(
    request: Request, project: ProjectName, name: Name, body: CronJobPause, user: CurrentUser
) -> CronJobOut:
    """Pause a cron job: it does not fire until resumed."""
    return await service.pause(request.app.state.engine, user, project, name, body.reason)


@router.post("/{project}/cron-jobs/{name}/resume", response_model=CronJobOut, responses=REFUSALS)
async def resume_cron_job(request: Request, project: ProjectName, name: Name, user: CurrentUser) -> CronJobOut:
    """Resume a paused cron job: it fires from its next minute on."""
    return await service.resume(request.app.state.engine, user, project, name)


@router.post("/{project}/cron-jobs/{name}/run", response_model=CronFireOut, responses=REFUSALS)
async def run_cron_job(request: Request, project: ProjectName, name: Name, user: CurrentUser) -> CronFireOut:
    """Fire a cron job now: a fire due at this moment, which the hub handles as any fire."""
    fire = await service.fire_now(request.app.state.engine, user, project, name)
    return service.fired(project, name, fire)


@router.delete("/{project}/cron-jobs/{name}", response_model=CronJobDeleted, responses=REFUSALS)
async def delete_cron_job(request: Request, project: ProjectName, name: Name, user: CurrentUser) -> CronJobDeleted:
    """Delete a cron job with its fires; its runs stay."""
    await service.delete(request.app.state.engine, user, project, name)
    return CronJobDeleted(project=project, deleted=name)
