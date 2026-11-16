"""Who holds the hub's pooled connections (``evo_agents.hub.db.TimedPool``): a connection held long is logged with the
request path that took it, and a PoolTimeout is logged with the connections out at that moment, each with its holder,
so the log of a burst of timeouts names what emptied the pool."""

import asyncio
import logging

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from psycopg_pool import PoolTimeout

from evo_agents.hub import db


def test_a_timeout_names_the_paths_that_hold_the_pool_and_a_long_hold_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(db, "HOLD_LOG_SECONDS", 0.2)
    caplog.set_level(logging.INFO, logger="evo_agents.hub.db")

    async def go():
        pool = db.TimedPool(pg.DSN, min_size=1, max_size=1, timeout=0.3, open=False)
        await pool.open(wait=True)
        try:
            taken, release = asyncio.Event(), asyncio.Event()

            async def slow():
                db.holder.set("GET /v1/slow")
                async with pool.connection():
                    taken.set()
                    await release.wait()

            holding = asyncio.create_task(slow(), name="slow-reader")
            await taken.wait()
            db.holder.set("POST /v1/worker/heartbeat")
            with pytest.raises(PoolTimeout):
                await pool.getconn()
            release.set()
            await holding
            async with pool.connection():  # given back: the pool serves again, and a quick hold is not logged
                pass
        finally:
            await pool.close()

    asyncio.run(go())
    timeout = next(r for r in caplog.records if r.getMessage() == "pool timeout")
    assert timeout.holder == "POST /v1/worker/heartbeat"
    assert [h["holder"] for h in timeout.out] == ["GET /v1/slow task:slow-reader"]
    assert timeout.out[0]["held_ms"] >= 300
    held = [r for r in caplog.records if r.getMessage() == "pool connection held"]
    assert [r.holder for r in held] == ["GET /v1/slow task:slow-reader"]
    assert held[0].held_ms >= 300
