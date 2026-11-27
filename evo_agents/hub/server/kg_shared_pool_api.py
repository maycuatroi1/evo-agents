"""The routes of the shared KG pool (``evo_agents.hub.server.kg_shared_pool``), a hub admin's
(``evo-agents hub admin kg-pool``):

- GET /v1/admin/kg-pool: the pool named now, its owner and the projects it serves; pool null while none is.
- PUT /v1/admin/kg-pool with ``owner`` and ``pool``: name that member's pool, in place of the one named before. An
  agent's pool, or a pool the owner does not have, is 422 and 404.
- DELETE /v1/admin/kg-pool: name none; the KG runs of projects with no repo wait for their agent's pool again.

Each change adds an audit row ``worker_pool.kg_shared``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg.models.shared_pool_api import KgSharedPool, KgSharedPoolSet
from evo_agents.hub.server.kg.service import shared_pool_api as service
from evo_agents.hub.server.security import AdminUser, admin

REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (404, 422)}

router = APIRouter(
    prefix="/v1/admin/kg-pool",
    tags=["admin"],
    dependencies=[Depends(admin)],
    responses={401: {"model": ErrorBody}, 403: {"model": ErrorBody}},
)


@router.get("", response_model=KgSharedPool)
async def show_kg_pool(request: Request) -> KgSharedPool:
    """The shared KG pool, its owner and the projects it serves."""
    return await service.show(request.app.state.engine)


@router.put("", response_model=KgSharedPool, responses=REFUSALS)
async def set_kg_pool(request: Request, body: KgSharedPoolSet, user: AdminUser) -> KgSharedPool:
    """Name a member's pool the shared KG pool, in place of the one named before."""
    return await service.name(request.app.state.engine, user, body.owner, body.pool)


@router.delete("", response_model=KgSharedPool)
async def clear_kg_pool(request: Request, user: AdminUser) -> KgSharedPool:
    """Name no shared KG pool."""
    return await service.clear(request.app.state.engine, user)
