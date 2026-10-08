"""The hub's Postgres access: one psycopg 3 ``AsyncConnectionPool`` per process, and a SQLAlchemy engine on it.

The lifespan of the api and of the worker opens the pool after migrating, then the engine on that pool
(``make_engine``); on shutdown it disposes of the engine, then closes the pool. The engine keeps no connection of its
own (``NullPool``): each one it hands out is taken from the pool (``async_creator=pool.getconn``), and closing it
gives it back, because the pool is made with ``close_returns=True``. The pool's size, timeout and check govern both,
and a connection is checked before it is handed out, so the pool recovers by itself after Postgres restarts. To
``NullPool`` each checkout is a new connection, so the dialect's connect hook runs on every one; what it changes on
the psycopg connection, the engine undoes as it gives the connection back (``make_engine``).

``engine.begin()`` gives an ``AsyncConnection`` whose transaction commits when the block ends cleanly and rolls
back otherwise; queries on it are SQLAlchemy Core on ``evo_agents.hub.tables``. ``driver(conn)`` is the psycopg
connection under it, in the same transaction: what procrastinate defers a job on (``evo_agents.hub.jobs``), and
nothing else; ``docs/hub.md`` (Data access) says how queries are written.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import psycopg
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import ColumnElement, any_, bindparam, column, event, func, literal, select, table
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql.psycopg import _log_notices
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
    Nothing connects until the first ``begin()``; dispose of it before closing the pool.

    The psycopg dialect's connect hook adds a notice handler, which logs what the server sends as NOTICE, to the
    connection it is given. To ``NullPool`` each checkout is a new connection, so the hook runs on every checkout of
    the same pooled connection, and its handlers would pile up: unbounded memory, and each notice logged once per
    handler. The engine takes the handler off as it closes the connection, which gives it back to the pool, so a
    pooled connection holds it only while the engine does."""
    engine = create_async_engine("postgresql+psycopg://", poolclass=NullPool, async_creator=pool.getconn)
    event.listen(engine.sync_engine.pool, "close", _undo_connect_hook)  # kept by dispose(), which recreates the pool
    return engine


def _undo_connect_hook(dbapi_connection, connection_record) -> None:
    """Undo, on a connection the engine is about to close, what the dialect's connect hook did to it. Runs on every
    close, an invalidated connection's included. An error here would keep the connection from going back to the
    pool, so a handler already gone is no error."""
    with suppress(ValueError):
        dbapi_connection.driver_connection.remove_notice_handler(_log_notices)


def one_of(target: ColumnElement, values=None, *, name: str | None = None) -> ColumnElement[bool]:
    """``target = ANY(array)``: true when ``target`` is one of ``values``, or of the list bound as ``name`` when
    the statement is built once and run with that list. An IN list is expanded into one parameter per value at
    each execution, which costs more than the query's round trip on the hot paths; an array is one parameter."""
    array = ARRAY(target.type)
    bound = bindparam(name, type_=array) if name is not None else literal(list(values), array)
    return target == any_(bound)


async def driver(conn: AsyncConnection) -> psycopg.AsyncConnection:
    """The psycopg connection under ``conn``, in the same transaction: what ``conn`` wrote, it sees, and what it
    writes commits or rolls back with ``conn``. Never commit, roll back or close it directly."""
    return (await conn.get_raw_connection()).driver_connection


async def schema_revision(engine: AsyncEngine, timeout: float) -> str | None:
    """The Alembic revision recorded in the database. ``timeout`` bounds the wait for a connection and the
    query together, so a health check cannot hang on a database that stopped answering; the statement gets
    ``timeout`` too, in its transaction only."""
    version = table("alembic_version", column("version_num")).c.version_num  # Alembic's table, not in tables.py

    async def query() -> str | None:
        async with engine.begin() as conn:
            await conn.execute(select(func.set_config("statement_timeout", f"{int(timeout * 1000)}ms", True)))
            revisions = (await conn.execute(select(version).order_by(version))).scalars().all()
        return ",".join(revisions) or None

    return await asyncio.wait_for(query(), timeout=timeout + 1)
