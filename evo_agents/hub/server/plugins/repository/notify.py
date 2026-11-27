"""The queries of a plugin's notices: the members of a project and their roles, and the keys of the notices sent
(table plugin_notice_keys)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def members(conn: AsyncConnection, project_id: int) -> list:
    """(id, login, role) of everyone with a grant on the project, by login."""
    g, u = tables.grants, tables.users
    query = (
        select(u.c.id, u.c.login, g.c.role)
        .join_from(g, u, u.c.id == g.c.user_id)
        .where(g.c.project_id == project_id)
        .order_by(u.c.login)
    )
    return list(await conn.execute(query))


async def claim_key(conn: AsyncConnection, project_id: int, plugin_id: int, key: str) -> bool:
    """Claim ``key`` for a send; False when a send of it was made already. A second claim waits for the first to
    commit."""
    k = tables.plugin_notice_keys
    claim = pg_insert(k).values(project_id=project_id, plugin_id=plugin_id, key=key, result={})
    claimed = await conn.execute(claim.on_conflict_do_nothing().returning(k.c.key))
    return claimed.first() is not None


async def first_result(conn: AsyncConnection, project_id: int, plugin_id: int, key: str) -> dict | None:
    k = tables.plugin_notice_keys
    query = select(k.c.result).where(k.c.project_id == project_id, k.c.plugin_id == plugin_id, k.c.key == key)
    return (await conn.execute(query)).scalar_one_or_none()


async def keep_result(conn: AsyncConnection, project_id: int, plugin_id: int, key: str, result: dict) -> None:
    k = tables.plugin_notice_keys
    await conn.execute(
        k.update().where(k.c.project_id == project_id, k.c.plugin_id == plugin_id, k.c.key == key).values(result=result)
    )
