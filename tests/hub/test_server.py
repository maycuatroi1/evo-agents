"""The hub's HTTP surface: health with the schema revision, 503 with a JSON body when Postgres stops,
liveness without the database, OpenAPI under /v1, JSON errors, the pool's life, and ``hub serve`` started
for real with a DSN whose password must not reach its log."""

import json
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import replace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient

from evo_agents import __version__
from evo_agents.hub.config import HubConfig
from evo_agents.hub.migrate import head_revision
from evo_agents.hub.server.app import create_app

HEAD = head_revision()


def make_config(db, tmp_path, **changes) -> HubConfig:
    config = HubConfig(dsn=db.dsn, data_dir=tmp_path / "cache", pool_min_size=1, pool_max_size=3, pool_timeout=5.0)
    return replace(config, **changes)


@pytest.fixture
def app(hub_db, tmp_path):
    return create_app(make_config(hub_db, tmp_path))


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


def is_json(response) -> bool:
    return response.headers["content-type"].startswith("application/json")


def test_health_reports_version_schema_and_db(client, tmp_path):
    response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        "schema": HEAD,
        "db": "ok",
        "r2": "unconfigured",  # without EVO_HUB_S3_*; tests/hub/test_blobs.py checks R2 itself
        "failed": [],
    }
    assert response.headers["cache-control"] == "no-store"
    assert (tmp_path / "cache").is_dir()


def test_health_is_503_with_a_json_body_while_postgres_is_down(client, hub_db):
    pg.set_reachable(hub_db, False)
    try:
        started = time.monotonic()
        response = client.get("/v1/health")
        assert time.monotonic() - started < 10
        assert response.status_code == 503 and is_json(response)
        assert response.json() == {
            "status": "unavailable",
            "version": __version__,
            "schema": None,
            "db": "unavailable",
            "r2": "unconfigured",
            "failed": ["db"],
        }
        live = client.get("/v1/health/live")  # the container stays up while the database is away
        assert live.status_code == 200 and live.json() == {"status": "ok"}
    finally:
        pg.set_reachable(hub_db, True)
    deadline = time.monotonic() + 30
    while (response := client.get("/v1/health")).status_code != 200:
        assert time.monotonic() < deadline, "the pool did not recover after Postgres came back"
        time.sleep(0.5)
    assert response.json()["schema"] == HEAD


def test_openapi_is_served_under_v1(client):
    response = client.get("/v1/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    assert spec["info"]["version"] == __version__
    assert {"/v1/health", "/v1/health/live"} <= set(spec["paths"])
    assert "503" in spec["paths"]["/v1/health"]["get"]["responses"]
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert client.get(path).status_code == 404


def test_errors_are_json_and_never_a_traceback(app, hub_db):
    def explode():
        raise RuntimeError("internal detail that must stay on the server")

    app.add_api_route("/v1/test-explode", explode)
    with TestClient(app, raise_server_exceptions=False) as client:
        signed_in = {"Authorization": f"Bearer {live.insert_token(hub_db, 'octo')}"}
        anonymous = client.get("/v1/nothing-here", headers={"X-Request-ID": "req-122"})
        assert anonymous.status_code == 401 and is_json(anonymous)  # /v1 fails closed, routed or not
        assert anonymous.json()["request_id"] == "req-122" and anonymous.headers["x-request-id"] == "req-122"

        missing = client.get("/v1/nothing-here", headers={"X-Request-ID": "req-123", **signed_in})
        assert missing.status_code == 404 and is_json(missing)
        assert missing.json() == {"error": "not_found", "message": "Not Found", "request_id": "req-123"}
        assert missing.headers["x-request-id"] == "req-123"

        wrong_method = client.post("/v1/health")
        assert wrong_method.status_code == 405 and is_json(wrong_method)
        assert wrong_method.json()["error"] == "method_not_allowed"

        failed = client.get("/v1/test-explode", headers={"X-Request-ID": "bad id with spaces", **signed_in})
        assert failed.status_code == 500 and is_json(failed)
        body = failed.json()
        assert body["error"] == "internal_error" and len(body["request_id"]) == 32
        assert "internal detail" not in failed.text and "Traceback" not in failed.text


def test_the_pool_is_bounded_and_closed_on_shutdown(app, hub_db):
    with TestClient(app) as client:
        pool = app.state.pool
        assert (pool.min_size, pool.max_size, pool.timeout) == (1, 3, 5.0)
        assert client.get("/v1/health").status_code == 200
        assert pg.backends(hub_db.name) >= 1
    assert pool.closed
    assert pg.wait_no_backends(hub_db.name) == 0


def test_the_app_does_not_start_when_postgres_is_unreachable(tmp_path):
    dsn = f"postgresql://hub:Start-Secret-8@127.0.0.1:{pg.free_port()}/hub"
    app = create_app(HubConfig(dsn=dsn, data_dir=tmp_path / "cache"))
    with pytest.raises(Exception, match="connection"), TestClient(app):
        pass
    assert not hasattr(app.state, "pool")


def get(url: str, timeout: float = 2.0):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, json.loads(response.read())


def test_serve_logs_json_without_the_dsn_password(hub_db, tmp_path):
    port = pg.free_port()
    env = pg.clean_env(EVO_HUB_DSN=hub_db.dsn, EVO_HUB_DATA_DIR=str(tmp_path / "cache"))
    proc = subprocess.Popen(
        [sys.executable, "-m", "evo_agents", "hub", "serve", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while True:
            assert proc.poll() is None, proc.communicate()[1]
            try:
                status, body = get(f"http://127.0.0.1:{port}/v1/health/live")
                break
            except OSError:
                assert time.monotonic() < deadline, "hub serve did not answer within 60s"
                time.sleep(0.2)
        assert (status, body) == (200, {"status": "ok"})
        assert get(f"http://127.0.0.1:{port}/v1/health")[1]["schema"] == HEAD
        proc.send_signal(signal.SIGTERM)
        _, stderr = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode in (0, -signal.SIGTERM), stderr  # uvicorn re-raises the signal once it has shut down
    assert hub_db.password not in stderr
    lines = pg.log_lines(stderr)
    (starting,) = [line for line in lines if line["msg"] == "hub starting"]
    assert starting["db"] == hub_db.dsn.replace(hub_db.password, "***")
    assert starting["version"] == __version__
    messages = [line["msg"] for line in lines]
    assert "migrations applied" in messages and "hub ready" in messages
    assert "hub stopped, connection pool closed" in messages


def test_serve_exits_nonzero_without_the_password_when_postgres_is_unreachable(tmp_path):
    dsn = f"postgresql://hub:Serve-Secret-9@127.0.0.1:{pg.free_port()}/hub"
    env = pg.clean_env(EVO_HUB_DSN=dsn, EVO_HUB_DATA_DIR=str(tmp_path / "cache"))
    result = pg.cli(["hub", "serve", "--port", str(pg.free_port())], env=env, timeout=60)
    assert result.returncode != 0
    assert "Serve-Secret-9" not in result.stderr
    lines = pg.log_lines(result.stderr)
    (failure,) = [line for line in lines if line["msg"] == "hub cannot start"]
    assert failure["db"] == dsn.replace("Serve-Secret-9", "***")
