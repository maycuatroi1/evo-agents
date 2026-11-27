"""The routes of the night shift (see the package): its charter, where it stands, its latest nights, and pausing and
resuming it. Each hands the request's parts and the app's engine to the service, which opens the transaction."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.curator.models.charter import (
    DEFAULT_NIGHTS,
    MAX_NIGHTS,
    Charter,
    CharterRevision,
    CharterWrite,
    CuratorStatus,
    NightList,
)
from evo_agents.hub.server.curator.service import charter as charters
from evo_agents.hub.server.curator.service import status as shift_status
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})


# Reading


@router.get(
    "/{project}/curator/charter",
    response_model=Charter,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def show_charter(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    revision: Annotated[int | None, Query(ge=1, le=2**31 - 1, description="an older revision")] = None,
) -> Charter:
    """The project's charter, at its newest revision or at ``revision``."""
    return await charters.show(request.app.state.engine, user, project, revision)


@router.get(
    "/{project}/curator/charter/revisions",
    response_model=list[CharterRevision],
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def charter_revisions(request: Request, project: ProjectName, user: CurrentUser) -> list[CharterRevision]:
    """Every revision of the project's charter, newest first; empty without one."""
    return await charters.revisions(request.app.state.engine, user, project)


# Writing


@router.put(
    "/{project}/curator/charter",
    response_model=Charter,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
)
async def write_charter(request: Request, project: ProjectName, body: CharterWrite, user: CurrentUser) -> Charter:
    """Write a new revision of the project's charter; the admins of the project alone may."""
    written, revision = await charters.write(request.app.state.engine, user, project, body)
    if revision is not None:
        log.info("charter written", extra={"project": project, "revision": revision, "login": user.login})
    return written


# Where the night shift stands


@router.get(
    "/{project}/curator",
    response_model=CuratorStatus,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def status(request: Request, project: ProjectName, user: CurrentUser) -> CuratorStatus:
    """The charter, the schedules and the night of the project's night shift."""
    return await shift_status.status(request.app.state.engine, user, project)


@router.get(
    "/{project}/curator/nights",
    response_model=NightList,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def nights(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=MAX_NIGHTS, description="the latest nights")] = DEFAULT_NIGHTS,
) -> NightList:
    """The latest nights of the project's night shift, each with its runs, their cost and its review run."""
    return await shift_status.nights(request.app.state.engine, user, project, limit)


# Pausing


@router.post(
    "/{project}/curator/pause",
    response_model=CuratorStatus,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
)
async def pause(request: Request, project: ProjectName, user: CurrentUser) -> CuratorStatus:
    """Pause every schedule of the project, and cancel the runs they queued that are still queued."""
    found, cancelled = await shift_status.pause(request.app.state.engine, user, project)
    log.info("night shift paused", extra={"project": project, "login": user.login, "cancelled": cancelled})
    return found


@router.post(
    "/{project}/curator/resume",
    response_model=CuratorStatus,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
)
async def resume(request: Request, project: ProjectName, user: CurrentUser) -> CuratorStatus:
    """Let every schedule of the project run again."""
    found = await shift_status.resume(request.app.state.engine, user, project)
    log.info("night shift resumed", extra={"project": project, "login": user.login})
    return found
