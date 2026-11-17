"""The hub's Postgres access: one psycopg 3 ``AsyncConnectionPool`` per process, and a SQLAlchemy engine on it.

The lifespan of the api and of the worker opens the pool after migrating, then the engine on that pool
(``make_engine``); on shutdown it disposes of the engine, then closes the pool. The engine keeps no connection of its
own (``NullPool``): each one it hands out is taken from the pool (``async_creator=pool.getconn``), and closing it
gives it back, because the pool is made with ``close_returns=True``. The pool's size, timeout and check govern both,
and a connection is checked before it is handed out, so the pool recovers by itself after Postgres restarts. A
Postgres that went away without closing its connections (its container killed on a stalled host, its address
dropped, no reset sent) leaves them open on this side, and a query on one would wait forever: the check gives up after
``CHECK_TIMEOUT``, and TCP keepalives (``CONNECT_KWARGS``) fail a connection whose peer stopped answering, so the pool
replaces it and reaches the Postgres that came back. To
``NullPool`` each checkout is a new connection, so the dialect's connect hook runs on every one; what it changes on
the psycopg connection, the engine undoes as it gives the connection back (``make_engine``).

``engine.begin()`` gives an ``AsyncConnection`` whose transaction commits when the block ends cleanly and rolls
back otherwise; queries on it are SQLAlchemy Core on ``evo_agents.hub.tables``. ``driver(conn)`` is the psycopg
connection under it, in the same transaction: what procrastinate defers a job on (``evo_agents.hub.jobs``), and
nothing else; ``docs/hub.md`` (Data access) says how queries are written.

The pool times each connection it hands out (``TimedPool``): one held ``HOLD_LOG_SECONDS`` or longer is logged as
``pool connection held`` with its holder, the request path the api's middleware set in ``holder`` and the name of the
task that took it, and a ``PoolTimeout`` is logged as ``pool timeout`` with the connections out at that moment, each
with its holder and how long it has held it: what the log says of who emptied the pool.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from contextvars import ContextVar
from time import monotonic

import psycopg
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from sqlalchemy import ColumnElement, any_, bindparam, column, event, func, literal, select, table
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql.psycopg import _log_notices
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from evo_agents.hub.config import HubConfig

log = logging.getLogger(__name__)

APPLICATION_NAME = "evo-agents-hub"
CONNECT_TIMEOUT = 10  # seconds for libpq to establish one connection
CHECK_TIMEOUT = 5.0  # seconds the check of a pooled connection may take before the pool drops it as dead
# libpq's TCP keepalives and user timeout: a connection whose peer stopped answering fails within about 25 s, idle or
# waiting for a reply, instead of waiting forever for a server that is gone
KEEPALIVES = {"keepalives": 1, "keepalives_idle": 10, "keepalives_interval": 5, "keepalives_count": 3}
CONNECT_KWARGS = {
    "application_name": APPLICATION_NAME,
    "connect_timeout": CONNECT_TIMEOUT,
    "tcp_user_timeout": 30_000,
    **KEEPALIVES,
}
HOLD_LOG_SECONDS = 2.0  # a pooled connection held this long or longer is logged with its holder (``TimedPool``)
HOLDERS_LOGGED = 30  # of the connections out when the pool times out, the longest held ones logged

# the request path, set by the api's middleware
holder: ContextVar[str] = ContextVar("evo_hub_pool_holder", default="-")


def _holder() -> str:
    """Who takes a connection now: ``holder``, and the name of the running task unless asyncio named it Task-<n>. A
    task inherits the ``holder`` of the request that started it, so a background load names the path it came from."""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    name = task.get_name() if task is not None else ""
    return holder.get() if not name or name.startswith("Task-") else f"{holder.get()} task:{name}"


class TimedPool(AsyncConnectionPool):
    """An ``AsyncConnectionPool`` that knows, for each connection out, who took it and when: it logs a connection
    held ``HOLD_LOG_SECONDS`` or longer as it comes back, and on a ``PoolTimeout`` the connections out then, the
    longest held first. ``connection()`` and a connection closed with ``close_returns`` go through ``getconn`` and
    ``putconn`` too."""

    def __init__(self, *args, **kwargs):
        self._out: dict[int, tuple[float, str]] = {}  # id of a connection out: (when it was taken, who took it)
        super().__init__(*args, **kwargs)

    async def getconn(self, timeout: float | None = None):
        who, asked = _holder(), monotonic()
        try:
            conn = await super().getconn(timeout)
        except PoolTimeout:
            log.warning(
                "pool timeout",
                extra={"holder": who, "waited_ms": _ms(monotonic() - asked), "out": self.holders()},
            )
            raise
        self._out[id(conn)] = (monotonic(), who)
        return conn

    async def putconn(self, conn) -> None:
        taken = self._out.pop(id(conn), None)
        if taken is not None and (held := monotonic() - taken[0]) >= HOLD_LOG_SECONDS:
            log.info("pool connection held", extra={"holder": taken[1], "held_ms": _ms(held)})
        await super().putconn(conn)

    def serving(self, within: float, but: str = "") -> bool:
        """Whether a connection taken ``within`` seconds ago or later is out, a request in the middle of its
        transaction, not counting the ones of a holder that names ``but``: what a build's thread running beside the
        loop (``evo_agents.hub.kg_snapshots``) lets go ahead of itself."""
        since = monotonic() - within
        return any(taken >= since and not (but and but in who) for taken, who in list(self._out.values()))

    def holders(self) -> list[dict]:
        """The connections out now, the longest held first: each one's holder and how long it has held it."""
        now = monotonic()
        out = sorted(self._out.values())[:HOLDERS_LOGGED]
        return [{"holder": who, "held_ms": _ms(now - taken)} for taken, who in out]


async def check_connection(conn) -> None:
    """The pool's check of a connection before it hands it out, bounded by ``CHECK_TIMEOUT``: a connection to a
    server that is gone raises instead of hanging, and the pool drops it and connects again."""
    await asyncio.wait_for(AsyncConnectionPool.check_connection(conn), CHECK_TIMEOUT)


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


def create_pool(config: HubConfig) -> AsyncConnectionPool:
    pool = TimedPool(
        config.dsn,
        min_size=config.pool_min_size,
        max_size=config.pool_max_size,
        timeout=config.pool_timeout,
        kwargs=CONNECT_KWARGS,
        check=check_connection,
        name="evo-hub",
        close_returns=True,  # a connection the engine closes goes back to the pool
        open=False,
    )
    live.append(pool)
    del live[:-1]
    return pool


live: list[TimedPool] = []  # the pool made last: the api's one


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
