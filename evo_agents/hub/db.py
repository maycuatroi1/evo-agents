"""The hub's Postgres access: one psycopg 3 ``AsyncConnectionPool`` per process, and a SQLAlchemy engine on it.

The lifespan of the api and of the worker opens the pool after migrating, then the engine on that pool
(``make_engine``); on shutdown it disposes of the engine, then closes the pool. The engine keeps no connection of its
own (``NullPool``): each one it hands out is taken from the pool (``async_creator=pool.getconn``), and closing it
gives it back, because the pool is made with ``close_returns=True``. The pool's size, timeout and check govern both,
and a connection is checked before it is handed out, so the pool recovers by itself after Postgres restarts.

``engine.begin()`` gives an ``AsyncConnection`` whose transaction commits when the block ends cleanly and rolls
back otherwise; queries on it are SQLAlchemy Core on ``evo_agents.hub.tables``. ``driver(conn)`` is the psycopg
connection under it, in the same transaction: what procrastinate defers a job on (``evo_agents.hub.jobs``).
``legacy(conn, query, params)`` runs a statement not yet written in Core on that connection, until none is left.
"""

from __future__ import annotations

import asyncio

import psycopg
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

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
        close_returns=True,  # a connection the engine closes goes back to the pool
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


def make_engine(pool: AsyncConnectionPool) -> AsyncEngine:
    """A SQLAlchemy engine whose connections are ``pool``'s, made with ``close_returns=True`` (``create_pool``).
    Nothing connects until the first ``begin()``; dispose of it before closing the pool."""
    return create_async_engine("postgresql+psycopg://", poolclass=NullPool, async_creator=pool.getconn)


async def driver(conn: AsyncConnection) -> psycopg.AsyncConnection:
    """The psycopg connection under ``conn``, in the same transaction: what ``conn`` wrote, it sees, and what it
    writes commits or rolls back with ``conn``. Never commit, roll back or close it directly."""
    return (await conn.get_raw_connection()).driver_connection


async def legacy(conn: AsyncConnection, query, params=None) -> psycopg.AsyncCursor:
    """Run ``query``, a SQL string not yet written in Core, on the psycopg connection under ``conn``, in its
    transaction. A bridge while the hub moves to Core: it goes once no module calls it."""
    return await (await driver(conn)).execute(query, params)


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
