"""The routes of the shared KG pool (``evo_agents.hub.server.kg_shared_pool``), a hub admin's
(``evo-agents hub admin kg-pool``):

- GET /v1/admin/kg-pool: the pool named now, its owner and the projects it serves; pool null while none is.
- PUT /v1/admin/kg-pool with ``owner`` and ``pool``: name that member's pool, in place of the one named before. An
  agent's pool, or a pool the owner does not have, is 422 and 404.
- DELETE /v1/admin/kg-pool: name none; the KG runs of projects with no repo wait for their agent's pool again.

Each change adds an audit row ``worker_pool.kg_shared``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import LOGIN_NAME
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import AdminUser, admin

REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (404, 422)}

router = APIRouter(
    prefix="/v1/admin/kg-pool",
    tags=["admin"],
    dependencies=[Depends(admin)],
    responses={401: {"model": ErrorBody}, 403: {"model": ErrorBody}},
)


class KgSharedPool(BaseModel):
    """The shared KG pool of the hub."""

    pool: str | None = Field(description="the pool named the shared KG pool; null while none is")
    owner: str | None = Field(description="the login of the member who owns it; null while none is")
    projects: list[str] = Field(description="the projects it serves, by name")


class KgSharedPoolSet(BaseModel):
    """The member's pool to name the shared KG pool."""

    model_config = ConfigDict(extra="forbid")

    owner: Annotated[str, Field(pattern=LOGIN_NAME, description="the login of the member who owns the pool")]
    pool: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$", description="the pool's name")]


async def _view(conn: AsyncConnection) -> KgSharedPool:
    p, u, pp, pj = tables.worker_pools, tables.users, tables.worker_pool_projects, tables.projects
    query = select(p.c.id, p.c.name, u.c.login).join_from(p, u, u.c.id == p.c.owner_id).where(p.c.kg_shared)
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return KgSharedPool(pool=None, owner=None, projects=[])
    served = select(pj.c.name).join_from(pp, pj, pj.c.id == pp.c.project_id).where(pp.c.pool_id == row.id)
    projects = sorted((await conn.execute(served)).scalars())
    return KgSharedPool(pool=row.name, owner=row.login, projects=projects)


async def _clear(conn: AsyncConnection, user: AdminUser, keep: int | None = None) -> None:
    p = tables.worker_pools
    named = update(p).values(kg_shared=False, updated_at=func.now()).where(p.c.kg_shared)
    if keep is not None:
        named = named.where(p.c.id != keep)
    for pool_id, name in (await conn.execute(named.returning(p.c.id, p.c.name))).all():
        target = f"pool:{pool_id} name={name} kg_shared=off"
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=audit.WORKER_POOL_KG_SHARED, target=target
        )


@router.get("", response_model=KgSharedPool)
async def show_kg_pool(request: Request) -> KgSharedPool:
    """The shared KG pool, its owner and the projects it serves."""
    async with request.app.state.engine.begin() as conn:
        return await _view(conn)


@router.put("", response_model=KgSharedPool, responses=REFUSALS)
async def set_kg_pool(request: Request, body: KgSharedPoolSet, user: AdminUser) -> KgSharedPool:
    """Name a member's pool the shared KG pool, in place of the one named before."""
    p, u = tables.worker_pools, tables.users
    query = (
        select(p.c.id, p.c.name, p.c.agent_id, p.c.kg_shared, u.c.login)
        .join_from(p, u, u.c.id == p.c.owner_id)
        .where(func.lower(u.c.login) == body.owner.lower(), func.lower(p.c.name) == body.pool.lower())
        .with_for_update(of=p)
    )
    async with request.app.state.engine.begin() as conn:
        row = (await conn.execute(query)).one_or_none()
        if row is None:
            raise HTTPException(404, f"{body.owner} has no pool {body.pool}")
        if row.agent_id is not None:
            raise HTTPException(422, f"pool {row.name} is an agent's: the shared KG pool is a member's pool")
        await _clear(conn, user, keep=row.id)
        if not row.kg_shared:
            await conn.execute(update(p).values(kg_shared=True, updated_at=func.now()).where(p.c.id == row.id))
            target = f"pool:{row.id} name={row.name} owner={row.login}"
            await audit.record(
                conn, actor_id=user.user_id, token_id=user.token_id, action=audit.WORKER_POOL_KG_SHARED, target=target
            )
        return await _view(conn)


@router.delete("", response_model=KgSharedPool)
async def clear_kg_pool(request: Request, user: AdminUser) -> KgSharedPool:
    """Name no shared KG pool."""
    async with request.app.state.engine.begin() as conn:
        await _clear(conn, user)
        return await _view(conn)
