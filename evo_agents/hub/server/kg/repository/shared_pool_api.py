"""The SQL of the shared KG pool (``evo_agents.hub.server.kg_shared_pool_api``): the pool named, the projects it
serves, and naming one or none (``worker_pools.kg_shared``)."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def named(conn: AsyncConnection):
    """(id, name, login of its owner) of the pool named the shared KG pool; None while none is."""
    p, u = tables.worker_pools, tables.users
    query = select(p.c.id, p.c.name, u.c.login).join_from(p, u, u.c.id == p.c.owner_id).where(p.c.kg_shared)
    return (await conn.execute(query)).one_or_none()


async def served(conn: AsyncConnection, pool_id: int) -> list[str]:
    """The names of the projects pool ``pool_id`` serves, sorted."""
    pp, pj = tables.worker_pool_projects, tables.projects
    query = select(pj.c.name).join_from(pp, pj, pj.c.id == pp.c.project_id).where(pp.c.pool_id == pool_id)
    return sorted((await conn.execute(query)).scalars())


async def unname(conn: AsyncConnection, keep: int | None = None) -> list[tuple[int, str]]:
    """Name no pool the shared KG pool but ``keep``; (id, name) of each pool that was."""
    p = tables.worker_pools
    query = update(p).values(kg_shared=False, updated_at=func.now()).where(p.c.kg_shared)
    if keep is not None:
        query = query.where(p.c.id != keep)
    return [(pool_id, name) for pool_id, name in (await conn.execute(query.returning(p.c.id, p.c.name))).all()]


async def member_pool(conn: AsyncConnection, owner: str, pool: str):
    """(id, name, agent_id, kg_shared, login) of pool ``pool`` of member ``owner``, either in any case, locked for the
    change; None for none."""
    p, u = tables.worker_pools, tables.users
    query = (
        select(p.c.id, p.c.name, p.c.agent_id, p.c.kg_shared, u.c.login)
        .join_from(p, u, u.c.id == p.c.owner_id)
        .where(func.lower(u.c.login) == owner.lower(), func.lower(p.c.name) == pool.lower())
        .with_for_update(of=p)
    )
    return (await conn.execute(query)).one_or_none()


async def name(conn: AsyncConnection, pool_id: int) -> None:
    """Name pool ``pool_id`` the shared KG pool."""
    p = tables.worker_pools
    await conn.execute(update(p).values(kg_shared=True, updated_at=func.now()).where(p.c.id == pool_id))
