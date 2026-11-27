"""The agent systems of a project (``docs/hub.md``, Agent systems; ``evo_agents.hub.system_spec``).

- GET /v1/projects/{p}/systems: its systems, by slug.
- GET .../systems/{slug}[?revision=N]: one, its spec at the revision (the latest by default), each pinned node with
  the latest version of its profile, and what is wrong in it now.
- GET .../systems/{slug}/revisions: its revisions, the newest first.
- PUT .../systems/{slug} {spec, if_revision}: creates it (no ``if_revision``, or 0) or stores the spec as its next
  revision (``if_revision`` its revision now; 409 ``revision_conflict`` otherwise, with the system as the hub holds
  it); a spec with an error is 422 with the problems as ``detail``, nothing written; a spec the hub holds already adds
  no revision.
- POST .../systems/validate {spec}: what is wrong in a spec, written nowhere.
- DELETE .../systems/{slug}: deletes it with its revisions; its profiles, graphs and runs stay.

A read opens the connection and hands the request to ``service.systems``, which says who may do what; a change hands
it the app's engine, and the service opens the transaction.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from evo_agents.hub.server.autonomy.service import guards as route_autonomy
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import CODES, ErrorBody
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.server.systems.models.systems import (
    WRITE_REFUSALS,
    Refused,
    Slug,
    SystemCheck,
    SystemChecked,
    SystemConflict,
    SystemDeleted,
    SystemOut,
    SystemPut,
    SystemRevision,
    SystemSummary,
    SystemWritten,
)
from evo_agents.hub.server.systems.service import systems as service

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/projects", tags=["systems"], dependencies=[route_autonomy.K12], responses={401: {"model": ErrorBody}}
)


def _refusal(request: Request, exc: Refused) -> JSONResponse:
    body = SystemConflict(
        error=exc.code or CODES.get(exc.status, f"http_{exc.status}"),
        message=exc.message,
        request_id=getattr(request.state, "request_id", None),
        detail=exc.detail,
        current=exc.current,
    )
    return JSONResponse(body.model_dump(mode="json", exclude_none=True), status_code=exc.status)


# Reading


@router.get(
    "/{project}/systems",
    response_model=list[SystemSummary],
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def list_systems(request: Request, project: ProjectName, user: CurrentUser) -> list[SystemSummary]:
    """The agent systems of the project, by slug."""
    async with request.app.state.engine.connect() as conn:
        return await service.listed(conn, user, project)


@router.post(
    "/{project}/systems/validate",
    response_model=SystemChecked,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def validate_system(request: Request, project: ProjectName, body: SystemCheck, user: CurrentUser):
    """What is wrong in a spec against the project, and the spec as PUT would store it; nothing is written."""
    async with request.app.state.engine.connect() as conn:
        return await service.validated(conn, user, project, body)


@router.get(
    "/{project}/systems/{slug}",
    response_model=SystemOut,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def show_system(
    request: Request,
    project: ProjectName,
    slug: Slug,
    user: CurrentUser,
    revision: Annotated[int | None, Query(ge=1, description="a revision of it; the latest by default")] = None,
) -> SystemOut:
    """One agent system, its spec at a revision, its pins and what is wrong in it now."""
    async with request.app.state.engine.connect() as conn:
        return await service.shown(conn, user, project, slug, revision)


@router.get(
    "/{project}/systems/{slug}/revisions",
    response_model=list[SystemRevision],
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def system_revisions(request: Request, project: ProjectName, slug: Slug, user: CurrentUser):
    """The revisions of an agent system, the newest first."""
    async with request.app.state.engine.connect() as conn:
        return await service.revisions(conn, user, project, slug)


# Changing


@router.put("/{project}/systems/{slug}", response_model=SystemWritten, responses=WRITE_REFUSALS)
async def put_system(request: Request, project: ProjectName, slug: Slug, body: SystemPut, user: CurrentUser):
    """Create the agent system, or store ``spec`` as its next revision."""
    spec = service.spec_in(slug, body)
    try:
        written = await service.put(request.app.state.engine, user, project, slug, body, spec)
    except Refused as exc:
        return _refusal(request, exc)
    if written.changed:
        log.info("agent system stored", extra={"project": project, "system": slug, "revision": written.revision})
    return written


@router.delete(
    "/{project}/systems/{slug}",
    response_model=SystemDeleted,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def delete_system(request: Request, project: ProjectName, slug: Slug, user: CurrentUser) -> SystemDeleted:
    """Delete an agent system with its revisions; its profiles, graphs and runs stay."""
    return await service.delete(request.app.state.engine, user, project, slug)
