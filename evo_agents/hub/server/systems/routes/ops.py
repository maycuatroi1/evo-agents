"""The default system of a project and the ops of a system (plan hub-steward, criteria a7 and a11; ``docs/hub.md``,
Agent systems).

- GET /v1/projects/{p}/default-system: the project's default system (``slug`` null when it has none), its ``ops`` and
  ``limits``.
- PUT /v1/projects/{p}/default-system {slug}: make that system the default, and no other (admin; audited as
  ``system.default``).
- GET /v1/projects/{p}/systems/{slug}/ops: the ops of a system: its spec's ``ops`` (``system_spec.ops_of``), its
  ``limits.ops_daily_usd``, whether they are paused, and the batches of the project's queue by state when it is the
  default.
- POST .../systems/{slug}/ops/pause {reason} and .../ops/resume: pause or resume them (admin; audited as
  ``system.ops-pause`` and ``system.ops-resume``). Paused, the queue still gathers; a closed batch opens no thread.
- GET .../systems/{slug}/ops/batches[?state=&limit=]: the batches of the project's queue, the newest first; empty for
  a system that is not the default.

Each route opens the connection and hands the request to ``service.ops``: every member of the project reads;
changing needs the admin role on it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from evo_agents.hub.server.autonomy.service import guards as route_autonomy
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.server.systems.models.ops import (
    REFUSALS,
    BatchState,
    DefaultPut,
    DefaultSystem,
    OpsBatch,
    OpsPause,
    SystemOps,
)
from evo_agents.hub.server.systems.models.systems import Slug
from evo_agents.hub.server.systems.service import ops as service

router = APIRouter(
    prefix="/v1/projects", tags=["systems"], dependencies=[route_autonomy.K12], responses={401: {"model": ErrorBody}}
)


# The default system


@router.get("/{project}/default-system", response_model=DefaultSystem, responses=REFUSALS)
async def show_default(request: Request, project: ProjectName, user: CurrentUser) -> DefaultSystem:
    """The project's default system, its ops and limits; ``slug`` null when it has none."""
    async with request.app.state.engine.connect() as conn:
        return await service.default_shown(conn, user, project)


@router.put("/{project}/default-system", response_model=DefaultSystem, responses=REFUSALS)
async def set_default(request: Request, project: ProjectName, body: DefaultPut, user: CurrentUser) -> DefaultSystem:
    """Make system ``slug`` the project's default system, and no other."""
    async with request.app.state.engine.begin() as conn:
        return await service.set_default(conn, user, project, body.slug)


# The ops of a system


@router.get("/{project}/systems/{slug}/ops", response_model=SystemOps, responses=REFUSALS)
async def show_ops(request: Request, project: ProjectName, slug: Slug, user: CurrentUser) -> SystemOps:
    """The ops of a system: its spec's ops, its ops budget, its pause, and the batches of its queue by state."""
    async with request.app.state.engine.connect() as conn:
        return await service.ops_shown(conn, user, project, slug)


async def _pause(request: Request, project: str, slug: str, user, reason: str | None) -> SystemOps:
    async with request.app.state.engine.begin() as conn:
        return await service.pause(conn, user, project, slug, reason)


@router.post("/{project}/systems/{slug}/ops/pause", response_model=SystemOps, responses=REFUSALS)
async def pause_ops(request: Request, project: ProjectName, slug: Slug, body: OpsPause, user: CurrentUser) -> SystemOps:
    """Pause the ops of a system: the queue still gathers, and a closed batch opens no thread until resumed."""
    reason = (body.reason or "").strip() or f"paused by {user.login}"
    return await _pause(request, project, slug, user, reason)


@router.post("/{project}/systems/{slug}/ops/resume", response_model=SystemOps, responses=REFUSALS)
async def resume_ops(request: Request, project: ProjectName, slug: Slug, user: CurrentUser) -> SystemOps:
    """Resume the ops of a system, paused by an admin or by the hub."""
    return await _pause(request, project, slug, user, None)


@router.get("/{project}/systems/{slug}/ops/batches", response_model=list[OpsBatch], responses=REFUSALS)
async def list_batches(
    request: Request,
    project: ProjectName,
    slug: Slug,
    user: CurrentUser,
    state: Annotated[BatchState | None, Query(description="only the batches in this state")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[OpsBatch]:
    """The batches of the project's queue, the newest first; none for a system that is not the default."""
    async with request.app.state.engine.connect() as conn:
        return await service.batches(conn, user, project, slug, state, limit)
