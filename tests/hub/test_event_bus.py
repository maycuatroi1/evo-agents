"""The optional event bus (``evo_agents.hub.bus``, ``evo_agents.hub.deliverer``): without EVO_HUB_BROKER_URL the hub
delivers through its jobs as before; with it, a commit's deliveries reach their plugin within seconds through RabbitMQ,
once each and in order, and nothing is lost while the broker is away.

The tests marked ``broker`` run against a real RabbitMQ named by EVO_HUB_TEST_BROKER_URL and skip without it; each
uses an exchange and queues of its own, so tests running at once on one broker never take each other's messages."""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Text, cast, func, select, update

from evo_agents.hub import bus, deliverer, jobs, plugin_events, tables
from evo_agents.hub.server.app import create_app
from tests.hub import live
from tests.hub.test_plugin_points import (  # noqa: F401 (fixtures)
    ASKED,
    HOST_TOKEN,
    _notify_owner,
    client,
    enable,
    fake,
    hub,
    on_hub,
)
from tests.hub.test_plugin_retry_after import QUEUED, _waiting
from tests.hub.test_runs import add_worker, claim, dispatched

BROKER = os.environ.get("EVO_HUB_TEST_BROKER_URL", "").strip()
broker = pytest.mark.skipif(not BROKER, reason="EVO_HUB_TEST_BROKER_URL is not set: these tests need RabbitMQ")
WITHIN = 5.0  # seconds from a commit to its plugin, with the bus


class FakeBroker:
    """A stand-in for ``deliverer.Broker``: it records what it published, or refuses while ``down``."""

    def __init__(self):
        self.published, self.down, self.ready = [], False, True

    async def publish(self, key: str, body: dict) -> None:
        if self.down:
            raise ConnectionError("the broker is away")
        self.published.append((key, body))

    async def consume(self, queue, key, handler) -> None:
        pass


def _deliverer(client, broker_, fake_=None) -> deliverer.Deliverer:
    app = client.app
    return deliverer.Deliverer(app.state.engine, broker_, config=app.state.config, host=fake_, dsn=app.state.config.dsn)


def _ids(db) -> list[int]:
    d = tables.plugin_deliveries
    return [row[0] for row in live.sql(db, select(d.c.id).order_by(d.c.id))]


def _states(db) -> list[str]:
    d = tables.plugin_deliveries
    return [row[0] for row in live.sql(db, select(d.c.state).order_by(d.c.id))]


def _committed(client, hub, db) -> list[int]:
    enable(client, hub["owner"])
    worker = add_worker(client, hub["owner"], "mac-mini", slots=3)
    dispatched(client, hub["owner"], [2, 4, 5])
    for _ in range(3):
        claim(client, worker)  # a run.state event each
    ids = _ids(db)
    assert len(ids) >= 2, "runs queued then one claimed: several run.state deliveries"
    return ids


def _clear_jobs(db) -> None:
    live.sql(db, QUEUED.delete().where(cast(QUEUED.c.status, Text) == "todo"))


def _soon_jobs(db, task: str) -> list[tuple]:
    return [row for row in _waiting(db, task) if row[1] == task]


# Without the broker (a1)


def test_without_the_broker_a_commit_queues_its_jobs_and_health_says_off(client, hub, hub_db, fake, monkeypatch):
    monkeypatch.delenv(bus.BROKER_VARIABLE, raising=False)
    assert not bus.on() and client.app.state.config.broker_url is None
    _committed(client, hub, hub_db)
    assert [row[0] for row in _soon_jobs(hub_db, jobs.DELIVER_PLUGIN_EVENTS)] == [jobs.DELIVERY_QUEUE]
    enable(client, hub["owner"], permissions=ASKED)
    on_hub(client, _notify_owner)
    assert _soon_jobs(hub_db, jobs.DELIVER_NOTIFICATIONS) == [(jobs.LEASE, jobs.DELIVER_NOTIFICATIONS, None)]
    found = client.get("/v1/health")
    assert found.status_code == 200 and found.json()["broker"] == "off" and found.json()["status"] == "ok"


def test_a_broker_url_that_is_not_amqp_is_refused_and_its_password_never_logged():
    from evo_agents.hub.config import ConfigError, load_config

    base = {"EVO_HUB_DSN": "postgresql://h@db/h"}
    with pytest.raises(ConfigError, match="amqp"):
        load_config({**base, bus.BROKER_VARIABLE: "http://rabbit:5672/"})
    config = load_config({**base, bus.BROKER_VARIABLE: "amqp://hub:s3cret-pass@rabbit:5672/"})
    assert config.broker_url == "amqp://hub:s3cret-pass@rabbit:5672/" and "s3cret" not in repr(config)
    from evo_agents.hub.log import scrub

    assert "s3cret-pass" not in scrub("connecting to amqp://hub:s3cret-pass@rabbit:5672/")


def test_health_reports_a_broker_away_as_error_and_stays_ok(hub_db, tmp_path, github):
    config = live.hub_config(hub_db, tmp_path, github, broker_url="amqp://guest:guest@127.0.0.1:1/")
    bus._checked.clear()
    with TestClient(create_app(config)) as client:
        found = client.get("/v1/health")
    body = found.json()
    assert found.status_code == 200 and body["broker"] == "error" and body["status"] == "ok" and body["failed"] == []


# With the bus: the relay, without a broker of its own


def test_with_the_bus_a_commit_wakes_the_relay_instead_of_queueing_a_job(client, hub, hub_db, fake, monkeypatch):
    _clear_jobs(hub_db)  # what the setup queued before the bus was on
    monkeypatch.setenv(bus.BROKER_VARIABLE, "amqp://guest:guest@127.0.0.1:1/")
    _committed(client, hub, hub_db)
    enable(client, hub["owner"], permissions=ASKED)
    on_hub(client, _notify_owner)
    assert _soon_jobs(hub_db, jobs.DELIVER_PLUGIN_EVENTS) == []
    assert _soon_jobs(hub_db, jobs.DELIVER_NOTIFICATIONS) == []


def test_the_relay_publishes_each_due_delivery_once_in_order_and_again_when_nobody_claimed_it(
    client, hub, hub_db, fake, monkeypatch
):
    monkeypatch.setenv(bus.BROKER_VARIABLE, "amqp://guest:guest@127.0.0.1:1/")
    ids = _committed(client, hub, hub_db)
    fake_broker = FakeBroker()
    relay = _deliverer(client, fake_broker, fake)
    assert on_hub(client, lambda app: relay.relay_once()) == {"published": len(ids), "fallback": 0}
    assert fake_broker.published == [("plugin.sample.run.state", {"id": i}) for i in ids]

    fake_broker.published.clear()
    on_hub(client, lambda app: relay.relay_once())
    assert fake_broker.published == [], "published once while it waits in the queue"

    d = tables.plugin_deliveries
    stale = func.now() - timedelta(seconds=plugin_events.REPUBLISH_SECONDS + 1)
    live.sql(hub_db, update(d).where(d.c.id == ids[0]).values(published_at=stale))
    on_hub(client, lambda app: relay.relay_once())
    assert fake_broker.published == [("plugin.sample.run.state", {"id": ids[0]})], "a message the broker lost"

    # A delivery tried and put off is published again once it falls due, not before.
    fake.answers["event"] = plugin_events.PluginCallError(-32603, "busy", retry_after=1)
    assert on_hub(client, lambda app: plugin_events.deliver_one(app.state.engine, ids[1], host=fake)) == "retried"
    fake_broker.published.clear()
    on_hub(client, lambda app: relay.relay_once())
    assert fake_broker.published == []
    live.sql(hub_db, update(d).where(d.c.id == ids[1]).values(next_at=func.now()))
    on_hub(client, lambda app: relay.relay_once())
    assert fake_broker.published == [("plugin.sample.run.state", {"id": ids[1]})]


def test_a_message_twice_or_beside_a_pass_delivers_once(client, hub, hub_db, fake, monkeypatch):
    monkeypatch.setenv(bus.BROKER_VARIABLE, "amqp://guest:guest@127.0.0.1:1/")
    ids = _committed(client, hub, hub_db)

    async def twice(app):
        engine = app.state.engine
        sent = [plugin_events.deliver_one(engine, i, host=fake) for i in ids for _ in range(2)]
        return await asyncio.gather(*sent, plugin_events.deliver(engine, host=fake, schedule=False))

    *outcomes, report = on_hub(client, twice)
    assert outcomes.count("delivered") + report["delivered"] == len(ids)
    delivered = sorted(call.params["delivery"] for call in fake.calls if call.method == "event")
    assert delivered == ids and _states(hub_db) == ["delivered"] * len(ids)
    assert on_hub(client, lambda app: plugin_events.deliver_one(app.state.engine, ids[0], host=fake)) is None


def test_while_the_broker_is_away_the_jobs_deliver_and_the_bus_carries_it_again_after(
    client, hub, hub_db, fake, monkeypatch
):
    monkeypatch.setenv(bus.BROKER_VARIABLE, "amqp://guest:guest@127.0.0.1:1/")
    ids = _committed(client, hub, hub_db)
    fake_broker = FakeBroker()
    fake_broker.down = True
    relay = _deliverer(client, fake_broker, fake)
    relay.notifications = True
    assert on_hub(client, lambda app: relay.relay_once()) == {"published": 0, "fallback": len(ids) + 1}
    assert [row[0] for row in _soon_jobs(hub_db, jobs.DELIVER_PLUGIN_EVENTS)] == [jobs.DELIVERY_QUEUE]
    assert [row[0] for row in _soon_jobs(hub_db, jobs.DELIVER_NOTIFICATIONS)] == [jobs.LEASE]
    d = tables.plugin_deliveries
    assert live.sql(hub_db, select(d.c.published_at).order_by(d.c.id)) == [(None,)] * len(ids)

    # The job queued then delivers everything due, as without the bus; the minute's pass leaves what is fresh.
    async def minute(app):
        return await plugin_events.deliver(
            app.state.engine, host=fake, schedule=False, grace=plugin_events.BUS_GRACE_SECONDS
        )

    assert on_hub(client, minute)["delivered"] == 0
    fake_broker.down = False
    on_hub(client, lambda app: relay.relay_once())
    assert [body["id"] for _, body in fake_broker.published] == ids, "published once the broker is back"


# With a real RabbitMQ (a2, a3, a6)


@pytest.fixture
def rabbit(monkeypatch):
    """A broker of the test's own: an exchange and queue names no other test uses, deleted after."""
    if not BROKER:
        pytest.skip("EVO_HUB_TEST_BROKER_URL is not set: these tests need RabbitMQ")
    tag = secrets.token_hex(4)
    monkeypatch.setenv(bus.BROKER_VARIABLE, BROKER)
    monkeypatch.setattr(bus, "EXCHANGE", f"test-{tag}.events")
    monkeypatch.setattr(bus, "QUEUE_PREFIX", f"test-{tag}.plugin.")
    monkeypatch.setattr(bus, "NOTIFICATIONS_QUEUE", f"test-{tag}.notifications")
    yield tag
    import aio_pika

    async def clean():
        connection = await aio_pika.connect(BROKER)
        async with connection:
            channel = await connection.channel()
            for name in (bus.queue_name("sample"), bus.NOTIFICATIONS_QUEUE):
                await channel.queue_delete(name)
            await channel.exchange_delete(bus.EXCHANGE)

    asyncio.run(clean())


def _start(client, fake_, url: str = BROKER):
    async def start(app):
        found = deliverer.Deliverer(
            app.state.engine,
            deliverer.Broker(url),
            config=app.state.config,
            host=fake_,
            dsn=app.state.config.dsn,
        )
        stop = asyncio.Event()
        task = asyncio.get_running_loop().create_task(found.run(stop))
        return found, stop, task

    return on_hub(client, start)


def _stop(client, started) -> None:
    found, stop, task = started

    async def end(app):
        stop.set()
        await task
        await found.broker.close()

    on_hub(client, end)


def _until(check, within: float = WITHIN) -> float:
    started = time.monotonic()
    while not check():
        assert time.monotonic() - started < within, f"not within {within} s"
        time.sleep(0.05)
    return time.monotonic() - started


def _ready(started) -> None:
    _until(lambda: started[0].broker.ready and bus.queue_name("sample") in started[0].broker._consumed, 15)


def _events(fake_) -> list[int]:
    return [call.params["delivery"] for call in fake_.calls if call.method == "event"]


@broker
def test_with_a_broker_events_reach_the_plugin_within_seconds_once_and_in_order(client, hub, hub_db, fake, rabbit):
    enable(client, hub["owner"])
    started = _start(client, fake)
    try:
        _ready(started)
        worker = add_worker(client, hub["owner"], "mac-mini", slots=3)
        dispatched(client, hub["owner"], [2, 4, 5])
        claim(client, worker)  # committed: a run.state event; no procrastinate worker runs in this test
        took = _until(lambda: len(_events(fake)) >= 1)
        assert took < WITHIN
        ids = _ids(hub_db)
        assert _events(fake) == ids, "in the order they were committed"

        # The same id published again is acked and dropped: the claim on the outbox decides.
        on_hub(client, lambda app: started[0].broker.publish(bus.routing_key("sample", "run.state"), {"id": ids[0]}))
        time.sleep(1.0)
        assert _events(fake) == ids and _states(hub_db) == ["delivered"] * len(ids)

        # Many at once keep their order through the plugin's single active consumer.
        claim(client, worker)
        claim(client, worker)
        _until(lambda: len(_events(fake)) == len(_ids(hub_db)), 15)
        assert _events(fake) == _ids(hub_db)
    finally:
        _stop(client, started)


@broker
def test_a_broker_away_loses_nothing_and_the_bus_carries_it_once_back(client, hub, hub_db, fake, rabbit, monkeypatch):
    enable(client, hub["owner"])
    away = _start(client, fake, "amqp://guest:guest@127.0.0.1:1/")
    try:
        worker = add_worker(client, hub["owner"], "mac-mini")
        dispatched(client, hub["owner"], [2])
        claim(client, worker)
        _until(lambda: _soon_jobs(hub_db, jobs.DELIVER_PLUGIN_EVENTS) != [])  # the jobs deliver meanwhile
        assert _events(fake) == []
    finally:
        _stop(client, away)
    _clear_jobs(hub_db)  # no job runs here: the bus must deliver
    started = _start(client, fake)
    try:
        _ready(started)
        _until(lambda: len(_events(fake)) == len(_ids(hub_db)), plugin_events.REPUBLISH_SECONDS + 15)
        assert _events(fake) == _ids(hub_db)
    finally:
        _stop(client, started)


@broker
def test_a_message_the_broker_lost_is_published_again_by_the_sweeper(client, hub, hub_db, fake, rabbit, monkeypatch):
    monkeypatch.setattr(plugin_events, "REPUBLISH_SECONDS", 2)
    enable(client, hub["owner"])
    d = tables.plugin_deliveries
    worker = add_worker(client, hub["owner"], "mac-mini")
    dispatched(client, hub["owner"], [2])
    claim(client, worker)
    live.sql(hub_db, update(d).values(published_at=func.now()))  # carried by a relay that died before publishing
    started = _start(client, fake)
    try:
        _ready(started)
        _until(lambda: len(_events(fake)) == len(_ids(hub_db)), 15)
        assert _states(hub_db) == ["delivered"] * len(_ids(hub_db))
    finally:
        _stop(client, started)


@broker
def test_with_a_broker_a_notification_goes_out_within_seconds(client, hub, hub_db, fake, rabbit):
    started = _start(client, fake)
    try:
        _until(lambda: started[0].broker.ready and bus.NOTIFICATIONS_QUEUE in started[0].broker._consumed, 15)
        on_hub(client, _notify_owner)
        n = tables.notification_deliveries
        pending = select(func.count()).select_from(n).where(n.c.state == "pending")
        _until(lambda: live.sql(hub_db, pending) == [(0,)])
        assert _soon_jobs(hub_db, jobs.DELIVER_NOTIFICATIONS) == [], "through the bus, not a job"
    finally:
        _stop(client, started)


def test_hub_deliver_refuses_to_start_without_the_broker(monkeypatch, capsys):
    from evo_agents.cli import main

    monkeypatch.delenv(bus.BROKER_VARIABLE, raising=False)
    monkeypatch.setenv("EVO_HUB_DSN", "postgresql://h@127.0.0.1:1/h")
    assert main(["hub", "deliver"]) == 2
    assert HOST_TOKEN  # the fixtures' module is loaded
