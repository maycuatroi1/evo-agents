"""The shared KG pool of the hub (``evo_agents.hub.server.kg_shared_pool_api``): what is named, and the one transaction
of naming a member's pool or none, each change audited as ``worker_pool.kg_shared``."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.server import audit
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg.models.shared_pool_api import KgSharedPool
from evo_agents.hub.server.kg.repository import shared_pool_api as repository


async def _view(conn: AsyncConnection) -> KgSharedPool:
    row = await repository.named(conn)
    if row is None:
        return KgSharedPool(pool=None, owner=None, projects=[])
    return KgSharedPool(pool=row.name, owner=row.login, projects=await repository.served(conn, row.id))


async def _record(conn: AsyncConnection, user, target: str) -> None:
    await audit.record(
        conn, actor_id=user.user_id, token_id=user.token_id, action=audit.WORKER_POOL_KG_SHARED, target=target
    )


async def _clear(conn: AsyncConnection, user, keep: int | None = None) -> None:
    for pool_id, name in await repository.unname(conn, keep):
        await _record(conn, user, f"pool:{pool_id} name={name} kg_shared=off")


async def show(engine: AsyncEngine) -> KgSharedPool:
    """The shared KG pool, its owner and the projects it serves."""
    async with UnitOfWork(engine) as conn:
        return await _view(conn)


async def name(engine: AsyncEngine, user, owner: str, pool: str) -> KgSharedPool:
    """Name pool ``pool`` of member ``owner`` the shared KG pool, in place of the one named before: 404 for a pool
    the owner does not have, 422 for an agent's."""
    async with UnitOfWork(engine) as conn:
        row = await repository.member_pool(conn, owner, pool)
        if row is None:
            raise HTTPException(404, f"{owner} has no pool {pool}")
        if row.agent_id is not None:
            raise HTTPException(422, f"pool {row.name} is an agent's: the shared KG pool is a member's pool")
        await _clear(conn, user, keep=row.id)
        if not row.kg_shared:
            await repository.name(conn, row.id)
            await _record(conn, user, f"pool:{row.id} name={row.name} owner={row.login}")
        return await _view(conn)


async def clear(engine: AsyncEngine, user) -> KgSharedPool:
    """Name no shared KG pool."""
    async with UnitOfWork(engine) as conn:
        await _clear(conn, user)
        return await _view(conn)
