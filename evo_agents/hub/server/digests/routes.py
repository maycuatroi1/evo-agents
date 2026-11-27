"""The routes of session digests: each opens the request's transaction and hands the request to
``service.sessions``; the package's docstring says what they do."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from pydantic import AwareDatetime

from evo_agents.hub.server.base.models import LOGIN_NAME, ProjectName
from evo_agents.hub.server.digests.models.sessions import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    READ_REFUSALS,
    REFUSALS,
    SCAN_ROWS,
    BoundedRoute,
    DigestList,
    DigestRecord,
    DigestSummary,
    SessionDigest,
    SessionId,
)
from evo_agents.hub.server.digests.service import sessions as service
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/projects", tags=["digests"], route_class=BoundedRoute, responses={401: {"model": ErrorBody}}
)


@router.put("/{project}/digests/{session_id}", response_model=DigestSummary, responses=REFUSALS)
async def put_digest(
    request: Request, project: ProjectName, session_id: SessionId, body: SessionDigest, user: CurrentUser
) -> DigestSummary:
    """Write the digest of session ``session_id``, replacing the one this member pushed before."""
    async with request.app.state.engine.begin() as conn:
        summary = await service.put(conn, user, project, session_id, body)
    log.info("session digest written", extra={"project": project, "session_id": session_id, "login": user.login})
    return summary


@router.get("/{project}/digests", response_model=DigestList, responses=READ_REFUSALS)
async def list_digests(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    since: Annotated[AwareDatetime | None, Query(description="pushed at or after this time")] = None,
    login: Annotated[str | None, Query(pattern=LOGIN_NAME, description="pushed by this member")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0, le=SCAN_ROWS)] = 0,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> DigestList:
    """The digests of the project the caller may read, the latest pushed first."""
    async with request.app.state.engine.begin() as conn:
        return await service.listed(
            conn, user, project, since=since, login=login, limit=limit, offset=offset, sink=sink
        )


@router.get("/{project}/digests/{session_id}", response_model=DigestRecord, responses=READ_REFUSALS)
async def show_digest(
    request: Request,
    project: ProjectName,
    session_id: SessionId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> DigestRecord:
    """The digest of session ``session_id``, when the caller may read it."""
    async with request.app.state.engine.begin() as conn:
        return await service.shown(conn, user, project, session_id, sink)
