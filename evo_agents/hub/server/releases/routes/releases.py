"""The routes of a project's daily release: its schedule, pausing it, checking now, and where it stands
(``evo_agents.hub.releases`` describes the model, ``evo_agents.hub.server.releases.service.check`` the check and its job).

GET /v1/projects/{p}/releases (reader) says where the release stands: the schedule, or null before an admin sets one;
whether it is paused; the moment of its next check; the latest checks, newest first, with the last one apart; and the
cycle of the schedule that is open or stopped, with its runs and their cost, for each run the version and branch it
pushed and how the hub landed it (its pull request, merge commit and tag, or why not), the tags it follows with the
state of their CI, the workflow run, the tests that failed and what kept the hub from reading it
(``actions_permission`` when the App's installation grants no actions), and for a stopped cycle why, the decision it
asks its owner and its thread in the triage chat.

PUT .../releases/schedule (admin of the project) sets the schedule: a key left out keeps what the schedule holds, or
takes its default (``releases.DEFAULT_*``) when there is none yet. The repo is one of the project's, on GitHub; with
no repo named and none set, the project's only repo on GitHub. The time zone must be one Postgres knows. The admin who
sets it becomes its owner, whom its runs are dispatched as. Audited as release.set.

POST .../releases/pause (admin) pauses the schedule: no check reads anything or opens a cycle until it is resumed,
the release runs of the project still queued are cancelled, and an open cycle left with no active run closes; a second
pause keeps the first one's time. POST .../releases/resume (admin) lets it check again. Neither touches the night
shift, nor does the night shift's pause, resume or circuit breaker touch them. Audited as release.pause and
release.resume.

POST .../releases/run (admin) makes a check now, through the same gates as the job's, and answers it; audited as
release.run. A member without the admin role gets 403 on each write, and a worker token 403 from the guard of /v1.
A run's token that acts as an admin reaches the schedule, resume and run only when its owner opened the run reach
``releases`` (``run_access_rules``); its check queues the cut as the daily check does, as the schedule's owner.

GET .../releases/cycles (reader) lists the cycles of the project that ended, published or closed, newest first, a page
of ``limit`` from ``offset``, each as the status shows the cycle open now: its runs, its tags and their CI, its cost
and why it ended. The web's Releases page lists them under the cycle open now.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.releases.models.releases import (
    MAX_CYCLES,
    MAX_CYCLES_OFFSET,
    SHOWN_CYCLES,
    ReleaseCheck,
    ReleaseCycleList,
    ReleaseScheduleWrite,
    ReleaseStatus,
)
from evo_agents.hub.server.releases.service import releases as service
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["releases"], responses={401: {"model": ErrorBody}})

WRITES = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 409: {"model": ErrorBody}}
READS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}


@router.get("/{project}/releases", response_model=ReleaseStatus, responses=READS)  # pyright: ignore[reportArgumentType]
async def status(request: Request, project: ProjectName, user: CurrentUser) -> ReleaseStatus:
    """Where the project's daily release stands: its schedule, its latest checks and the cycle open now."""
    return await service.status(request.app.state.engine, user, project)


@router.get("/{project}/releases/cycles", response_model=ReleaseCycleList, responses=READS)  # pyright: ignore[reportArgumentType]
async def ended_cycles(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=MAX_CYCLES, description="the cycles of one page")] = SHOWN_CYCLES,
    offset: Annotated[int, Query(ge=0, le=MAX_CYCLES_OFFSET, description="the cycles before the page")] = 0,
) -> ReleaseCycleList:
    """The project's release cycles that ended, published or closed, newest first, each with its runs and tags."""
    return await service.ended(request.app.state.engine, user, project, limit, offset)


@router.put(
    "/{project}/releases/schedule", response_model=ReleaseStatus, responses={**WRITES, 422: {"model": ErrorBody}}
)
async def set_schedule(
    request: Request, project: ProjectName, body: ReleaseScheduleWrite, user: CurrentUser
) -> ReleaseStatus:
    """Set the project's release schedule; the caller becomes its owner."""
    return await service.set_schedule(request.app.state.engine, user, project, body)


@router.post("/{project}/releases/pause", response_model=ReleaseStatus, responses=WRITES)  # pyright: ignore[reportArgumentType]
async def pause(request: Request, project: ProjectName, user: CurrentUser) -> ReleaseStatus:
    """Pause the release schedule, cancel the release runs still queued and close an open cycle left idle."""
    return await service.pause(request.app.state.engine, user, project)


@router.post("/{project}/releases/resume", response_model=ReleaseStatus, responses=WRITES)  # pyright: ignore[reportArgumentType]
async def resume(request: Request, project: ProjectName, user: CurrentUser) -> ReleaseStatus:
    """Let the release schedule check again."""
    return await service.resume(request.app.state.engine, user, project)


@router.post("/{project}/releases/run", response_model=ReleaseCheck, responses=WRITES)  # pyright: ignore[reportArgumentType]
async def run_now(request: Request, project: ProjectName, user: CurrentUser) -> ReleaseCheck:
    """Check now, through the gates of the daily check, and answer the check."""
    return await service.run_now(request.app.state.engine, request.app.state.github_app, user, project)
