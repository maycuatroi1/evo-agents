"""The hub's Postgres access: one psycopg 3 ``AsyncConnectionPool`` per process and hand-written SQL.

The FastAPI lifespan opens the pool after migrating and closes it on shutdown. Every query runs through
``pool.connection()``, whose transaction commits when the block ends cleanly and rolls back otherwise.
A connection is checked before it is handed out, so the pool recovers by itself after Postgres restarts.
"""

from __future__ import annotations

import asyncio

from psycopg_pool import AsyncConnectionPool

from evo_agents.hub.config import HubConfig

APPLICATION_NAME = "evo-agents-hub"
CONNECT_TIMEOUT = 10  # seconds for libpq to establish one connection


def create_pool(config: HubConfig) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        config.dsn,
        min_size=config.pool_min_size,
        max_size=config.pool_max_size,
        timeout=config.pool_timeout,
        kwargs={"application_name": APPLICATION_NAME, "connect_timeout": CONNECT_TIMEOUT},
        check=AsyncConnectionPool.check_connection,
        name="evo-hub",
        open=False,
    )


async def open_pool(config: HubConfig) -> AsyncConnectionPool:
    """An open pool holding its minimum of connections; raises PoolTimeout when Postgres does not answer."""
    pool = create_pool(config)
    try:
        await pool.open(wait=True, timeout=config.pool_timeout)
    except BaseException:
        await pool.close()
        raise
    return pool


async def schema_revision(pool: AsyncConnectionPool, timeout: float) -> str | None:
    """The Alembic revision recorded in the database. ``timeout`` bounds the wait for a connection and the
    query together, so a health check cannot hang on a database that stopped answering."""

    async def query() -> str | None:
        async with pool.connection(timeout=timeout) as conn:
            await conn.execute("SELECT set_config('statement_timeout', %s, true)", (f"{int(timeout * 1000)}ms",))
            cursor = await conn.execute("SELECT version_num FROM alembic_version ORDER BY version_num")
            rows = await cursor.fetchall()
        return ",".join(row[0] for row in rows) or None

    return await asyncio.wait_for(query(), timeout=timeout + 1)
