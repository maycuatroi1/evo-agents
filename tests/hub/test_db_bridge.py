"""The hub's SQLAlchemy engine on its psycopg pool (``evo_agents.hub.db``).

SQLAlchemy and the psycopg connection under it (``driver``) work in one transaction: each sees the other's rows, both
report the same transaction id, and both commit or roll back together. A connection the engine used goes back to the
pool idle. jsonb, timestamptz, bytea, bigint[] and text[] read through SQLAlchemy as they read through psycopg, which
the code not yet on the engine relies on. A job deferred on an engine connection exists only once its transaction
commits, and a defer refused because one waits already leaves the transaction usable.

The statements psycopg runs here are SQLAlchemy statements compiled by the engine's dialect: no SQL is written out.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb
from sqlalchemy import BigInteger, Column, DateTime, Integer, LargeBinary, MetaData, Table, Text, func, insert, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import jobs, tables
from evo_agents.hub.config import HubConfig
from evo_agents.hub.db import driver, make_engine, open_pool
from evo_agents.hub.jobs import JobQueue
from evo_agents.hub.migrate import migrate

# A table of this test's own, holding each type the hub keeps and the bridge must read the same either way.
SAMPLES = Table(
    "bridge_samples",
    MetaData(),
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("doc", JSONB),
    Column("at", DateTime(timezone=True)),
    Column("data", LargeBinary),
    Column("ids", ARRAY(BigInteger)),
    Column("names", ARRAY(Text)),
)
VALUES = [
    {
        "doc": {"run": {"steps": [1, 2, {"verify": None, "ok": True}], "cost": 0.25}, "note": "Tiếng Việt có dấu ✓"},
        "at": datetime(2026, 10, 8, 9, 30, 15, 123456, tzinfo=timezone(timedelta(hours=7))),
        "data": bytes(range(256)) + b"\x00",
        "ids": [1, 2**62, -5],
        "names": ["a", "Tiếng Việt", "", "x,y", 'q"uote', "{brace}"],
    },
    {
        "doc": [1, "two", {"three": 3.5}, False, None, []],
        "at": datetime(1999, 12, 31, 23, 59, 59, tzinfo=UTC),
        "data": b"",
        "ids": [],
        "names": [],
    },
    {
        "doc": "chuỗi Unicode: Tiếng Việt, 日本語, emoji 🚀",
        "at": datetime(2026, 2, 28, 0, 0, tzinfo=timezone(timedelta(hours=-3, minutes=-30))),
        "data": "đ".encode(),
        "ids": [0],
        "names": ["日本語"],
    },
]
# The part of procrastinate's job table this test reads.
QUEUE = Table(
    "procrastinate_jobs",
    MetaData(),
    Column("id", BigInteger),
    Column("task_name", Text),
    Column("lock", Text),
    Column("queueing_lock", Text),
    Column("args", JSONB),
    Column("status", Text),
)


class Rollback(Exception):
    """Raised to leave an ``engine.begin()`` block, which then rolls back."""


@asynccontextmanager
async def hub(db, max_size: int = 2):
    """The pool and the engine on it, as the api opens and closes them."""
    pool = await open_pool(HubConfig(dsn=db.dsn, data_dir=Path("."), pool_min_size=1, pool_max_size=max_size))
    engine = make_engine(pool)
    try:
        yield pool, engine
    finally:
        await engine.dispose()
        await pool.close()


async def on_driver(conn: AsyncConnection, engine: AsyncEngine, statement) -> list[tuple]:
    """``statement`` run on the psycopg connection under ``conn``, compiled by the engine's dialect."""
    compiled = statement.compile(dialect=engine.dialect)
    params = {
        name: Jsonb(value) if isinstance(compiled.binds[name].type, JSONB) else value
        for name, value in compiled.params.items()
    }
    cursor = await (await driver(conn)).execute(compiled.string, params)
    return await cursor.fetchall() if cursor.description else []


def logins():
    return select(tables.users.c.login).order_by(tables.users.c.login)


def test_sqlalchemy_and_the_driver_connection_share_one_transaction(hub_db):
    migrate(hub_db.dsn)

    async def check():
        async with hub(hub_db) as (pool, engine):
            async with engine.begin() as conn:
                await conn.execute(insert(tables.users).values(login="alice"))
                assert await on_driver(conn, engine, logins()) == [("alice",)]
                await on_driver(conn, engine, insert(tables.users).values(login="bob"))
                assert (await conn.execute(logins())).all() == [("alice",), ("bob",)]
                ours = (await conn.execute(select(func.txid_current()))).scalar_one()
                assert await on_driver(conn, engine, select(func.txid_current())) == [(ours,)]
                assert (await driver(conn)).info.transaction_status == TransactionStatus.INTRANS
            with pytest.raises(Rollback):  # both roll back together
                async with engine.begin() as conn:
                    await conn.execute(insert(tables.users).values(login="carol"))
                    await on_driver(conn, engine, insert(tables.users).values(login="dave"))
                    raise Rollback
            async with engine.connect() as conn:  # a connection of its own: what committed, and only that
                return (await conn.execute(logins())).all()

    assert asyncio.run(check()) == [("alice",), ("bob",)]


def test_the_pool_gets_back_every_connection_the_engine_used(hub_db, caplog):
    migrate(hub_db.dsn)

    async def check():
        async with hub(hub_db, max_size=3) as (pool, engine):
            idle = pool.get_stats()["pool_available"]
            async with engine.begin() as conn:
                await conn.execute(select(func.txid_current()))
                taken = pool.get_stats()["pool_available"]
            after_commit = pool.get_stats()["pool_available"]
            with pytest.raises(Rollback):
                async with engine.begin() as conn:
                    await conn.execute(insert(tables.users).values(login="erin"))
                    raise Rollback
            after_rollback = pool.get_stats()["pool_available"]

            async def one(n: int) -> None:
                async with engine.begin() as conn:
                    await conn.execute(insert(tables.users).values(login=f"user-{n}"))

            await asyncio.gather(*(one(n) for n in range(12)))  # more than the pool holds: they wait their turn
            for _ in range(50):  # a connection the pool grew may still be on its way in
                stats = pool.get_stats()
                if stats["pool_available"] == stats["pool_size"]:
                    break
                await asyncio.sleep(0.1)
            async with pool.connection() as conn:  # what the code on pool.connection() gets next is idle
                status = conn.info.transaction_status
            return idle, taken, after_commit, after_rollback, stats, status

    with caplog.at_level(logging.WARNING, logger="psycopg.pool"):
        idle, taken, after_commit, after_rollback, stats, status = asyncio.run(check())
    assert idle == 1 and taken == 0 and after_commit == after_rollback == 1
    assert stats["pool_size"] <= 3 and stats["pool_available"] == stats["pool_size"]
    assert status == TransactionStatus.IDLE
    assert [r.getMessage() for r in caplog.records if r.name.startswith("psycopg.pool")] == []


def test_types_read_through_sqlalchemy_as_through_psycopg(hub_db):
    migrate(hub_db.dsn)

    async def check():
        async with hub(hub_db) as (pool, engine):
            async with engine.begin() as conn:
                await conn.run_sync(SAMPLES.metadata.create_all)
                # rows 1 to 3 written with SQLAlchemy, 11 to 13 with psycopg, as the code not on the engine writes
                await conn.execute(insert(SAMPLES), [{"id": n, **v} for n, v in enumerate(VALUES, 1)])
                for n, value in enumerate(VALUES, 11):
                    await on_driver(conn, engine, insert(SAMPLES).values(id=n, **value))
            query = select(SAMPLES).order_by(SAMPLES.c.id)
            async with engine.begin() as conn:
                through_sqlalchemy = [tuple(row) for row in (await conn.execute(query)).all()]
                through_psycopg = await on_driver(conn, engine, query)
            return through_sqlalchemy, through_psycopg

    through_sqlalchemy, through_psycopg = asyncio.run(check())
    assert through_sqlalchemy == through_psycopg
    expected = [(n, *value.values()) for n, value in [*enumerate(VALUES, 1), *enumerate(VALUES, 11)]]
    assert through_sqlalchemy == expected
    for row, psycopg_row in zip(through_sqlalchemy, through_psycopg, strict=True):
        assert [type(value) for value in row] == [type(value) for value in psycopg_row]
        assert row[2].utcoffset() == psycopg_row[2].utcoffset()  # the session's time zone, either way
        assert isinstance(row[3], bytes)


def test_a_job_deferred_on_an_engine_connection_exists_only_once_it_commits(hub_db):
    migrate(hub_db.dsn)

    async def check():
        async with hub(hub_db, max_size=3) as (pool, engine):
            queue = await JobQueue.open(pool)
            async with engine.begin() as conn:
                kept = await queue.defer_kg(jobs.PING, "alpha", connection=conn, note="kept")
                async with engine.connect() as other:  # not visible outside the transaction before it commits
                    unseen = (await other.execute(select(QUEUE.c.id))).all()
            with pytest.raises(Rollback):
                async with engine.begin() as conn:
                    await queue.defer_kg(jobs.PING, "beta", connection=conn)
                    raise Rollback
            async with engine.begin() as conn:  # alpha waits already: refused, and the transaction goes on
                refused = await queue.defer_kg(jobs.PING, "alpha", connection=conn, note="again")
                await conn.execute(insert(tables.users).values(login="after-the-refusal"))
                other = await queue.defer_kg(jobs.PING, "gamma", connection=conn)
            async with engine.connect() as conn:
                queued = (await conn.execute(select(QUEUE).order_by(QUEUE.c.id))).all()
                users = (await conn.execute(logins())).all()
            return kept, unseen, refused, other, queued, users

    kept, unseen, refused, other, queued, users = asyncio.run(check())
    assert isinstance(kept, int) and unseen == [] and refused is None and isinstance(other, int)
    assert [tuple(row) for row in queued] == [
        (kept, jobs.PING, "kg:alpha", "kg:alpha", {"note": "kept"}, "todo"),
        (other, jobs.PING, "kg:gamma", "kg:gamma", {}, "todo"),
    ]
    assert users == [("after-the-refusal",)]
