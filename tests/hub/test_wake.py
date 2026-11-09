"""Wake-ups between the hub's processes (``evo_agents.hub.wake``, ``evo_agents.hub.server.listen``): without
EVO_HUB_BROKER_URL a wake-up is Postgres's NOTIFY, as before; with it, it goes out on the exchange ``hub.wake`` once
the transaction committed, and every api process's listener takes it, so a run queued in one wakes the claim waiting
in another.

The tests marked by the ``rabbit`` fixture run against a real RabbitMQ named by EVO_HUB_TEST_BROKER_URL and skip
without it; each uses an exchange of its own, deleted after.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
import time

import httpx
import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from evo_agents.hub import bus, wake
from evo_agents.hub.server import listen
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.runs.service import claims as run_claims
from tests.hub import live
from tests.hub.test_event_bus import BROKER
from tests.hub.test_run_stream import serving, start  # noqa: F401 (fixture)
from tests.hub.test_runs import OWNER, add_worker, claim, dispatched

CHANNEL = "evo_test_wake"


def _engine(db):
    return create_async_engine(db.dsn.replace("postgresql://", "postgresql+psycopg://", 1))


async def _write(engine, *wakes: tuple[str, str], fail: bool = False) -> None:
    """One transaction that wakes ``wakes`` and commits, or raises and rolls back when ``fail``."""
    try:
        async with engine.begin() as conn:
            await conn.execute(text("select 1"))
            for channel, payload in wakes:
                await wake.notify(conn, channel, payload)
            if fail:
                raise RuntimeError("rolled back")
    except RuntimeError:
        pass


def test_without_the_broker_a_wake_up_is_a_notify_that_arrives_once_the_transaction_commits(hub_db, monkeypatch):
    monkeypatch.delenv(bus.BROKER_VARIABLE, raising=False)

    async def main():
        engine = _engine(hub_db)
        listening = await psycopg.AsyncConnection.connect(hub_db.dsn, autocommit=True)
        try:
            await listening.execute(f"LISTEN {CHANNEL}")
            await _write(engine, (CHANNEL, "7"), fail=True)
            await _write(engine, (CHANNEL, "8"), (CHANNEL, "8"))
            got = []
            async for notify in listening.notifies(timeout=1.0):
                got.append(notify.payload)
            return got
        finally:
            await listening.close()
            await engine.dispose()

    assert asyncio.run(main()) == ["8"]  # the rolled back one never came, the equal two came as one


@pytest.fixture
def rabbit(monkeypatch):
    """The bus on, with a wake exchange no other test uses, deleted after."""
    if not BROKER:
        pytest.skip("EVO_HUB_TEST_BROKER_URL is not set: these tests need RabbitMQ")
    monkeypatch.setenv(bus.BROKER_VARIABLE, BROKER)
    monkeypatch.setattr(wake, "EXCHANGE", f"test-{secrets.token_hex(4)}.wake")
    yield BROKER
    import aio_pika

    async def clean():
        connection = await aio_pika.connect(BROKER)
        async with connection:
            channel = await connection.channel()
            await channel.exchange_delete(wake.EXCHANGE)

    asyncio.run(clean())


async def _until(condition, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"{what} did not hold within {timeout}s"
        await asyncio.sleep(0.02)


def test_with_the_broker_every_listener_gets_a_wake_up_after_the_commit_and_none_of_a_rollback(hub_db, rabbit):
    async def main():
        engine = _engine(hub_db)
        got: dict[str, list] = {"one": [], "two": []}
        listeners = []
        for name in got:  # two api processes, each with its own queue on the exchange
            listener = listen.Listener(hub_db.dsn, rabbit)
            listener.on(CHANNEL, got[name].append)
            listener.on("evo_test_other", lambda payload: None)
            listener.start()
            listeners.append(listener)
        try:
            await _until(lambda: all(None in each for each in got.values()), 10, "the listeners to connect")
            for each in got.values():
                each.clear()
            async with engine.begin() as conn:
                await conn.execute(text("select 1"))
                await wake.notify(conn, CHANNEL, "5")
                await wake.notify(conn, CHANNEL, "5")
                await asyncio.sleep(0.5)
                assert got == {"one": [], "two": []}, "nothing goes out before the commit"
            committed = time.monotonic()
            await _until(lambda: all(got.values()), 2, "the wake-up to reach both listeners")
            assert time.monotonic() - committed < 2
            await _write(engine, (CHANNEL, "6"), fail=True)
            await _write(engine, ("evo_test_other", "9"))
            await _write(engine, (CHANNEL, "7"))
            await _until(lambda: all("7" in each for each in got.values()), 2, "the next wake-up")
            assert got == {"one": ["5", "7"], "two": ["5", "7"]}  # folded, and nothing of the rollback
        finally:
            for listener in listeners:
                await listener.close()
            await wake.close()
            await engine.dispose()

    asyncio.run(main())


def test_with_the_broker_a_run_queued_on_one_api_process_is_claimed_on_another_within_two_seconds(
    start, hub_db, tmp_path, github, rabbit, monkeypatch
):
    monkeypatch.setattr(run_claims, "CLAIM_POLL_SECONDS", 60.0)  # only the wake-up can wake the claim in time
    one = start(broker_url=rabbit)
    other = create_app(live.hub_config(hub_db, tmp_path, github, broker_url=rabbit))
    with serving(other) as url, httpx.Client(base_url=url, timeout=30) as http:
        worker = add_worker(http, one.headers["owner"], "mac-mini")
        assert worker and OWNER
        answer = {}

        def wait():
            answer["run"] = claim(http, worker, wait=20)
            answer["at"] = time.monotonic()

        thread = threading.Thread(target=wait)
        thread.start()
        time.sleep(2.0)  # the claim looks, and again once the listener of the other process is on the broker
        queued = time.monotonic()
        run = dispatched(one.client, one.headers["owner"], [2])[0]
        thread.join(25)
        assert answer["run"]["id"] == run["id"]
        assert answer["at"] - queued < 2, "the run is claimed within two seconds of being queued"
