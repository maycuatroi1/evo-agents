"""The hub's cache and its rate limits (``evo_agents.hub.cache``, ``docs/hub.md``, section Cache): in memory without
EVO_HUB_REDIS_URL and on Redis with it, get, set with a TTL and a drop by prefix under a namespace; a write to a table
an area is read from drops the area, in the writing process at the commit and in every other by the wake-up on
``cache.CHANNEL``; a sliding window shared between processes; a Redis away that only makes the hub slower; and the
``cache`` field of /v1/health, which never makes the status an error.

The two read paths the cache serves (the guard policy, the notification count) are each checked against both kinds of
write: the hub's own, and another process's, heard through the listener only. The Redis checks need
EVO_HUB_TEST_REDIS_URL (a Redis they may write keys under a prefix of their own to); a Redis away needs none.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import time
import uuid

import pytest
from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects import postgresql

from evo_agents.hub import cache, tables
from evo_agents.hub.cache import HUB, Cache, LocalWindow, Window
from tests.hub import pg

if pg.DSN:  # the guard's fixtures; without Postgres, hub_db skips the hub's checks
    from tests.hub.test_guard import app_key, guard_core  # noqa: F401

REDIS = os.environ.get("EVO_HUB_TEST_REDIS_URL") or None
AWAY = "redis://127.0.0.1:1/0"  # nothing listens there: each call fails at once


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeListener:
    def __init__(self, up: bool = True):
        self.up, self.started, self.handlers = up, 0, {}

    def on(self, channel, handler):
        self.handlers[channel] = handler

    def start(self):
        self.started += 1


@pytest.fixture
def redis_url(monkeypatch):
    """Redis's URL, with the keys of this test under a prefix of its own, removed after it."""
    if REDIS is None:
        pytest.skip("EVO_HUB_TEST_REDIS_URL is not set")
    prefix = f"evo:test:{uuid.uuid4().hex}:"
    monkeypatch.setattr(cache, "PREFIX", prefix)
    yield REDIS

    async def clean():
        import redis.asyncio

        client = redis.asyncio.Redis.from_url(REDIS)
        async for key in client.scan_iter(match=prefix + "*"):
            await client.unlink(key)
        await client.aclose()

    asyncio.run(clean())


def run(coro):
    return asyncio.run(coro)


# The cache in memory


def test_memory_keeps_a_value_for_its_ttl_under_its_namespace_and_drops_by_prefix():
    clock = Clock()
    store = Cache(clock=clock)

    async def go():
        assert await store.check() == "off"
        await store.set(HUB, "guard:policy", {"terms": [1]}, ttl=10)
        await store.set("acme", "guard:policy", "acme's", ttl=10)
        await store.set(HUB, "notifications:count:1", 3, ttl=10)
        assert await store.get(HUB, "guard:policy") == {"terms": [1]}
        assert await store.get("acme", "guard:policy") == "acme's"
        assert await store.get("other", "guard:policy", "none") == "none"
        clock.now += 10
        assert await store.get(HUB, "guard:policy") is None  # past its TTL
        assert await store.get(HUB, "notifications:count:1") is None
        await store.set(HUB, "notifications:count:1", 3, ttl=10)
        await store.set(HUB, "notifications:count:2", 4, ttl=10)
        await store.set(HUB, "guard:policy", "p", ttl=10)
        await store.drop(HUB, "notifications:count:")
        assert await store.get(HUB, "notifications:count:2") is None
        assert await store.get(HUB, "guard:policy") == "p"
        await store.set("acme", "guard:x", 1, ttl=10)
        await store.drop("acme")
        assert await store.get("acme", "guard:x") is None
        assert await store.get(HUB, "guard:policy") == "p"  # another namespace

    run(go())


def test_a_value_loaded_while_its_area_is_dropped_is_answered_and_not_kept():
    store = Cache(clock=Clock())
    loads = []

    async def load():
        loads.append(1)
        if len(loads) == 1:
            store.heard("guard")  # a write committed while Postgres was read
        return len(loads)

    async def go():
        assert await store.fetch(HUB, "guard:policy", 60, load) == 1
        assert await store.fetch(HUB, "guard:policy", 60, load) == 2  # the first was not kept
        assert await store.fetch(HUB, "guard:policy", 60, load) == 2

    run(go())


def test_a_write_or_a_reconnection_drops_the_values_and_a_listener_down_bypasses_the_cache():
    listener = FakeListener()
    store = Cache(listener=listener, clock=Clock())
    assert cache.CHANNEL in listener.handlers
    loads = []

    async def load():
        loads.append(1)
        return len(loads)

    async def go():
        assert await store.fetch(HUB, "guard:policy", 60, load) == 1
        assert await store.fetch(HUB, "guard:policy", 60, load) == 1
        store.wrote("guard")  # this process committed a write of the area
        assert await store.fetch(HUB, "guard:policy", 60, load) == 2
        listener.handlers[cache.CHANNEL]("notifications")  # another area
        assert await store.fetch(HUB, "guard:policy", 60, load) == 2
        listener.handlers[cache.CHANNEL](None)  # the listener (re)connected: anything may have been missed
        assert await store.fetch(HUB, "guard:policy", 60, load) == 3
        listener.up = False
        assert await store.fetch(HUB, "guard:policy", 60, load) == 4
        assert await store.fetch(HUB, "guard:policy", 60, load) == 5
        assert listener.started == 2

    run(go())


def test_a_write_of_a_watched_table_names_its_areas_a_cte_and_a_cascade_included():
    import evo_agents.hub.guard  # noqa: F401  the areas are watched where they are read
    import evo_agents.hub.server.notifications  # noqa: F401

    def written(statement):
        return cache._written(statement, statement.compile(dialect=postgresql.dialect()))

    assert written(insert(tables.guard_terms).values(term="x")) == {"guard"}
    assert written(update(tables.notifications).values(read_at=None)) == {"notifications"}
    assert written(delete(tables.grants)) == {"notifications"}
    assert written(select(tables.guard_terms)) == set()
    in_cte = update(tables.notifications).values(read_at=None).returning(tables.notifications.c.id).cte("marked")
    assert written(select(in_cte.c.id)) == {"notifications"}
    assert {"guard", "notifications"} <= written(delete(tables.projects))  # their rows go with the project's


# Rate limits


def test_a_local_window_slides():
    clock = Clock()
    window = LocalWindow(limit=2, window=10, clock=clock)
    assert window.retry_after("a") is None
    window.hit("a")
    clock.now += 4
    window.hit("a")
    assert window.retry_after("a") == 6 and window.retry_after("b") is None
    clock.now += 6
    assert window.retry_after("a") is None  # the first hit left the window
    window.hit("a")
    assert window.retry_after("a") == 4


def test_without_redis_the_window_counts_in_memory():
    window = Window(Cache(), "test", LocalWindow(limit=1, window=60))

    async def go():
        assert await window.retry_after("a") is None
        await window.hit("a")
        assert 0 < await window.retry_after("a") <= 60

    run(go())


# On Redis


def test_redis_shares_values_drops_and_the_window_between_processes(redis_url):
    one, two = Cache(redis_url), Cache(redis_url)
    windows = [Window(store, "join", LocalWindow(limit=2, window=60)) for store in (one, two)]

    async def go():
        assert await one.check() == "ok"
        await one.set(HUB, "guard:policy", {"terms": ["h"]}, ttl=30)
        await one.set("acme", "guard:policy", "acme's", ttl=30)
        assert await two.get(HUB, "guard:policy") == {"terms": ["h"]}
        assert await two.get("acme", "guard:policy") == "acme's"
        await two.drop(HUB, "guard:")
        assert await one.get(HUB, "guard:policy") is None
        await one.set(HUB, "short:x", 1, ttl=0.05)
        await asyncio.sleep(0.15)
        assert await two.get(HUB, "short:x") is None  # past its TTL
        loads = []

        async def load():
            loads.append(1)
            return len(loads)

        assert await one.fetch(HUB, "guard:policy", 30, load) == 1
        assert await two.fetch(HUB, "guard:policy", 30, load) == 1  # one load for both processes
        two.heard("guard")  # two's listener heard a write
        await asyncio.sleep(0.05)
        assert await one.fetch(HUB, "guard:policy", 30, load) == 2
        await windows[0].hit("10.0.0.1")
        await windows[1].hit("10.0.0.1")
        assert 0 < await windows[0].retry_after("10.0.0.1") <= 60  # two hits, one from each process
        assert await windows[1].retry_after("10.0.0.2") is None
        assert windows[0].local.retry_after("10.0.0.1") is None  # counted on Redis, not in memory
        for store in (one, two):
            await store.close()

    run(go())


def test_with_redis_away_the_cache_misses_at_once_and_the_window_counts_in_memory():
    store = Cache(AWAY)
    window = Window(store, "join", LocalWindow(limit=1, window=60))
    loads = []

    async def load():
        loads.append(1)
        return len(loads)

    async def go():
        assert await store.fetch(HUB, "guard:policy", 30, load) == 1
        started = time.monotonic()
        assert await store.fetch(HUB, "guard:policy", 30, load) == 2  # still right: read again
        await store.set(HUB, "x:y", 1, ttl=30)
        assert await store.get(HUB, "x:y") is None
        await store.drop(HUB, "x:")
        assert time.monotonic() - started < 0.2  # left alone while broken, not waited on
        await window.hit("10.0.0.1")
        assert await window.retry_after("10.0.0.1") is not None
        assert await store.check() == "error"
        await store.close()

    run(go())


# The hub


def listening(client, timeout: float = 10.0) -> None:
    """Return once the app's listener listens, so the cache is used, with nothing kept; the writes before it are taken
    as heard."""
    deadline = time.monotonic() + timeout
    while not client.app.state.listener.up:
        assert time.monotonic() < deadline, "the listener did not start"
        time.sleep(0.02)
    client.app.state.cache._dirty.clear()
    client.portal.call(client.app.state.cache.drop, HUB)  # a test counts its loads from an empty cache


def eventually(check, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "the write was not heard"
        time.sleep(0.05)


def without_commit_drops(client, monkeypatch) -> None:
    """Make the app's cache hear writes only through its listener, as another process's would."""
    monkeypatch.setattr(cache, "_caches", type(cache._caches)())
    assert client.app.state.cache not in cache._caches


@pytest.fixture(params=["memory", "redis", "away"])
def backend(request):
    """The hub's Redis: none, the test's, or one that never answers (the hub then reads Postgres each time)."""
    if request.param == "redis":
        return request.getfixturevalue("redis_url")
    return AWAY if request.param == "away" else None


@pytest.fixture
def guard_client(hub_db, tmp_path, github, app_key, guard_core, backend):
    from fastapi.testclient import TestClient

    from evo_agents.hub.server.app import create_app
    from tests.hub import test_credentials_api as credentials_api

    config = dataclasses.replace(credentials_api.hub_config(hub_db, tmp_path, github, app_key), redis_url=backend)
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


def test_the_guard_policy_is_cached_and_dropped_by_a_write_here_or_elsewhere(
    guard_client, github, hub_db, monkeypatch, backend
):
    from evo_agents.hub import guard
    from tests.hub import test_credentials_api as credentials_api
    from tests.hub.live import sql
    from tests.hub.test_guard import LISTS, guarded_project
    from tests.hub.test_runs import beat

    client = guard_client
    hub = guarded_project(client, github)
    worker = credentials_api.worker_of(client, hub["owner"], "box")
    loads, real = [], guard.policy

    async def counted(conn):
        loads.append(1)
        return await real(conn)

    monkeypatch.setattr(guard, "policy", counted)

    def policy() -> dict:
        response = client.get("/v1/guard/policy", headers=hub["other"])
        assert response.status_code == 200, response.text
        return response.json()

    policy()
    listening(client)
    loads.clear()
    assert policy()["terms"] == [] and beat(client, worker)["settings"]["guard"] == ""
    assert policy()["terms"] == []
    assert len(loads) == (3 if backend == AWAY else 1)  # one read of Postgres for three asks

    client.post(f"{LISTS}/terms", json={"term": "Northwind"}, headers=hub["owner"])
    client.post(f"{LISTS}/targets", json={"pattern": "acme/public"}, headers=hub["owner"])
    now = policy()  # this process wrote it: dropped at the commit
    assert len(now["terms"]) == len(guard.private_names.entries("Northwind")) and now["targets"]
    assert beat(client, worker)["settings"]["guard"] == now["digest"]
    assert len(loads) == (5 if backend == AWAY else 2)

    without_commit_drops(client, monkeypatch)
    sql(hub_db, delete(tables.guard_targets))  # another process's write, heard through the listener only
    eventually(lambda: policy()["targets"] == [])
    assert beat(client, worker)["settings"]["guard"] == ""


@pytest.fixture
def notice_client(hub_db, tmp_path, github, backend):
    from fastapi.testclient import TestClient

    from evo_agents.hub.server.app import create_app
    from tests.hub import live

    config = dataclasses.replace(live.hub_config(hub_db, tmp_path, github), redis_url=backend)
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


def test_the_notification_count_is_cached_and_dropped_by_a_write_here_or_elsewhere(
    notice_client, github, hub_db, monkeypatch, backend
):
    from evo_agents.hub.server import notifications
    from tests.hub.live import sql
    from tests.hub.test_notifications import alert, count, read
    from tests.hub.test_plan_runs import plan_body, push, started
    from tests.hub.test_runs import members

    client = notice_client
    hub = members(client, github)
    push(client, hub["owner"], plan_body())
    worker, run_ = started(client, hub)
    loads, real = [], notifications._counts

    def counted(user_id):
        loads.append(user_id)
        return real(user_id)

    monkeypatch.setattr(notifications, "_counts", counted)
    count(client, hub["owner"])
    listening(client)
    loads.clear()
    assert count(client, hub["owner"])["unread"] == 0
    assert count(client, hub["owner"])["unread"] == 0
    assert len(loads) == (2 if backend == AWAY else 1)

    alert(client, worker, run_["id"])  # a new notice
    assert count(client, hub["owner"])["unread"] == 1
    assert read(client, hub["owner"], all=True).json()["read"] == 1
    assert count(client, hub["owner"])["unread"] == 0
    assert count(client, hub["owner"]) == {"unread": 0, "open_decisions": 0, "open_proposals": 0}

    without_commit_drops(client, monkeypatch)
    sql(hub_db, update(tables.notifications).values(read_at=None))  # another process's write
    eventually(lambda: count(client, hub["owner"])["unread"] == 1)


@pytest.mark.parametrize(("url", "field"), [(None, "off"), (AWAY, "error"), ("redis", "ok")])
def test_health_reports_the_cache_and_its_error_leaves_the_status_ok(hub_db, tmp_path, request, url, field):
    from fastapi.testclient import TestClient

    from evo_agents.hub.server.app import create_app
    from tests.hub.test_server import make_config

    if url == "redis":
        url = request.getfixturevalue("redis_url")
    with TestClient(create_app(make_config(hub_db, tmp_path, redis_url=url))) as client:
        response = client.get("/v1/health")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok" and response.json()["cache"] == field


def test_the_redis_url_is_checked():
    assert cache.redis_url({}) is None
    assert cache.redis_url({"EVO_HUB_REDIS_URL": " redis://cache:6379/0 "}) == "redis://cache:6379/0"
    assert cache.redis_url({"EVO_HUB_REDIS_URL": "unix:///run/redis.sock"}) == "unix:///run/redis.sock"
    for wrong in ("http://cache:6379", "cache:6379", "redis://"):
        with pytest.raises(cache.RedisUrlError):
            cache.redis_url({"EVO_HUB_REDIS_URL": wrong})
