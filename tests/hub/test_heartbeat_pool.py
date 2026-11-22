"""The api's pool of the workers' heartbeats and claims (``evo_agents.hub.db.HEARTBEAT_PATHS``): while something holds
every connection of the main pool, as a graph's load or a burst of requests stalled behind it did, a worker's
heartbeat and claim, the check of its token included, still get a connection, and /v1/health says which pool is
empty."""

from __future__ import annotations

import time

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient  # noqa: E402

from evo_agents.hub.server.app import create_app  # noqa: E402
from tests.hub import live  # noqa: E402
from tests.hub.test_runs import add_worker, beat, claim, hub  # noqa: E402, F401 (fixtures)

# The main pool's wait for a connection: a heartbeat or a claim that took its connection from the main pool would wait
# this long, while one of the heartbeat pool answers in a fraction of it, on a loaded CI runner too (a bound of 1 s left
# it little room, and the test failed in the CI of v0.73.0)
POOL_TIMEOUT = 3.0


@pytest.fixture
def client(hub_db, tmp_path, github):
    config = live.hub_config(
        hub_db, tmp_path, github, pool_max_size=2, pool_timeout=POOL_TIMEOUT, heartbeat_pool_size=2
    )
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


def test_heartbeats_and_claims_get_a_connection_while_the_main_pool_is_held(client, hub):  # noqa: F811
    worker = add_worker(client, hub["owner"], "mac-mini")
    pool = client.app.state.pool
    held = [client.portal.call(pool.getconn) for _ in range(pool.max_size)]  # a load holding the whole main pool
    try:
        started = time.monotonic()
        beat(client, worker)
        assert claim(client, worker) is None  # nothing queued: the claim answered, it did not time out on the pool
        assert time.monotonic() - started < POOL_TIMEOUT  # under the pool's timeout: neither waited for the main pool
        health = client.get("/v1/health")
        assert health.status_code == 503
        body = health.json()
        assert (body["db"], body["heartbeat_db"], body["failed"]) == ("unavailable", "ok", ["db"])
        assert body["pools"]["api"]["available"] == 0 and body["pools"]["heartbeat"]["max"] == 2
    finally:
        for conn in held:
            client.portal.call(pool.putconn, conn)
    body = client.get("/v1/health").json()
    assert (body["status"], body["heartbeat_db"]) == ("ok", "ok")
