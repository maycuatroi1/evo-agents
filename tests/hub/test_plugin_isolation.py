"""The secret boundary of isolated plugins on the hub side (criterion a4 of hub-agent-plugins):

- a plugin's backend token carries only what its installed version asks for and the projects granted
  (``plugin_tokens.issue_token``, ``backend_payload``), and starting a backend again revokes the plugin's older tokens;
- the ``initialize`` payload of one plugin carries only that plugin's config, its secrets decrypted here where the key
  is, and never another plugin's secret;
- a crash or a hang of a backend makes the plugin host answer an error within the timeout (``plugin_host_service``),
  and the api's client to the host answers an error within the timeout when the host is unreachable;
- the plugin host reports each backend's state, and a hub admin reads it at ``GET /v1/plugins/{name}/backend``.

The DB checks need the hub (they skip without EVO_HUB_TEST_DSN); the host and client checks run a real backend
subprocess and need no DB.
"""

from __future__ import annotations

import asyncio
import secrets
from pathlib import Path

import pytest

import tests.hub.test_plugin_projects as projects
from tests.hub import live, pg
from tests.hub.test_plugin_projects import ASKED, PROJECT, install, prepare

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "plugins"


# The host and client checks (no DB)


def _manifest(name: str):
    from evo_agents.plugins.manifest import read_manifest

    return read_manifest((FIXTURES / name / "evo-plugin.json").read_bytes())


def _bundle(name: str) -> bytes:
    from evo_agents.plugins.bundle import pack

    return pack(FIXTURES / name).data


def _init(name: str, plugin_id: int = 1) -> dict:
    return {
        "protocol": "plugin/1",
        "plugin": {"id": name, "version": "1.0.0"},
        "plugin_id": plugin_id,
        "hub": {"url": "http://127.0.0.1:9/", "token": "evp_test", "version": "0.0.0"},
        "config": {"instance": {}, "projects": {}},
        "grants": {},
    }


def _host_app(tmp_path, token: str):
    from fastapi.testclient import TestClient

    from evo_agents.hub.plugin_host_service import create_host_app
    from evo_agents.plugins.host import Host

    host = Host(tmp_path, allow_shared_user=True)
    host.place(_manifest("faulty"), _bundle("faulty"), _init("faulty"))
    return TestClient(create_host_app(host, token)), host


def test_plugin_host_crash_and_hang_answer_an_error_within_the_timeout(tmp_path):
    client, host = _host_app(tmp_path, "shared-secret")
    auth = {"Authorization": "Bearer shared-secret"}

    assert client.post("/v1/host/plugins/faulty/call", json={"method": "ping"}).status_code == 401  # no secret
    ok = client.post("/v1/host/plugins/faulty/call", json={"method": "tool", "params": _call("ok")}, headers=auth)
    assert ok.status_code == 200 and ok.json()["result"]["content"][0]["text"] == "ok"

    hung = client.post(
        "/v1/host/plugins/faulty/call",
        json={"method": "tool", "params": _call("hang"), "timeout": 1.0},
        headers=auth,
    )
    assert hung.status_code == 503, hung.text  # the hang did not hold the request open past its timeout

    crashed = client.post(
        "/v1/host/plugins/faulty/call", json={"method": "tool", "params": _call("crash")}, headers=auth
    )
    assert crashed.status_code == 503, crashed.text
    status = {b["plugin"]: b for b in client.get("/v1/host/backends", headers=auth).json()["backends"]}
    assert status["faulty"]["state"] == "backoff" and status["faulty"]["restarts"] >= 1
    asyncio.run(host.stop_all())


def _call(tool: str) -> dict:
    return {"tool": tool, "arguments": {}, "caller": {"project": "p"}}


def test_plugin_host_client_crash_is_unavailable_without_a_host_and_within_the_timeout():
    from evo_agents.hub.server.plugin_host import PluginHostClient, PluginHostUnavailable

    async def unconfigured():
        with pytest.raises(PluginHostUnavailable):
            await PluginHostClient(None, None).call("sample", "ping", {}, timeout=1.0)

    async def unreachable():
        client = PluginHostClient("http://127.0.0.1:1", "secret")
        with pytest.raises(PluginHostUnavailable):
            await client.call("sample", "ping", {}, timeout=1.0)
        await client.aclose()

    asyncio.run(unconfigured())
    asyncio.run(unreachable())


# The DB checks

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient  # noqa: E402

from evo_agents.hub.server.app import create_app  # noqa: E402
from evo_agents.hub.server.sealing import KEY_BYTES  # noqa: E402

HOST_TOKEN = "plugin-host-shared-" + secrets.token_hex(16)


@pytest.fixture
def client(hub_db, tmp_path, github):
    config = live.hub_config(
        hub_db, tmp_path, github, secrets_key=secrets.token_bytes(KEY_BYTES), plugin_host_token=HOST_TOKEN
    )
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github, hub_db) -> dict:
    return prepare(client, github, hub_db)


def _run(hub_db, fn):
    import psycopg
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    async def go():
        engine = create_async_engine(
            "postgresql+psycopg://",
            async_creator=lambda: psycopg.AsyncConnection.connect(hub_db.dsn),
            poolclass=NullPool,
        )
        try:
            async with engine.begin() as conn:
                return await fn(conn)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _plugin_row(conn, name: str):
    from sqlalchemy import select

    from evo_agents.hub import tables

    p, v = tables.plugins, tables.plugin_versions
    query = (
        select(p.c.id, p.c.name, p.c.installed_version, v.c.manifest)
        .join_from(p, v, (v.c.plugin_id == p.c.id) & (v.c.version == p.c.installed_version))
        .where(p.c.name == name)
    )
    return query


def test_plugin_token_isolation_carries_only_granted_permissions(client, hub, hub_db):

    from evo_agents.hub.server.plugin_tokens import backend_payload, issue_token, plugin_of_token

    projects.enabled(client, hub["owner"], ASKED)  # grant exactly what sandbox asks for

    async def mint(conn):
        row = (await conn.execute(_plugin_row(conn, "sandbox"))).one()
        first = await issue_token(conn, row.id)
        second = await issue_token(conn, row.id)
        payload = await backend_payload(conn, None, "https://hub.test", second, row)
        return first, second, payload

    first, second, payload = _run(hub_db, mint)
    assert payload["grants"] == {PROJECT: ASKED}
    assert payload["permissions"] == ASKED
    assert payload["plugin"] == {"id": "sandbox", "version": "1.0.0"}

    async def live_tokens(conn):
        return await plugin_of_token(conn, first), await plugin_of_token(conn, second)

    old, new = _run(hub_db, live_tokens)
    assert old is None and new is not None  # starting a backend again revoked the older token


def test_plugin_token_isolation_a_disabled_plugin_grants_nothing(client, hub, hub_db):
    from evo_agents.hub.server.plugin_tokens import backend_payload

    projects.enabled(client, hub["owner"], ASKED)
    projects.turn_off(hub_db, "incompatible")  # the hub turned it off

    async def payload(conn):
        row = (await conn.execute(_plugin_row(conn, "sandbox"))).one()
        return await backend_payload(conn, None, "https://hub.test", "evp_x", row)

    assert _run(hub_db, payload)["grants"] == {}


def test_plugin_config_isolation_one_plugin_never_sees_another_secret(client, hub, hub_db):
    from evo_agents.hub.server.plugin_tokens import backend_payload

    install(hub_db, name="beta")  # a second installed plugin
    # The hub's own (instance) layer holds a secret for each plugin.
    for name, token in (("sandbox", "sandbox-only-secret"), ("beta", "beta-only-secret")):
        written = client.post(
            f"/v1/plugins/{name}/config",
            json={"values": {"greeting": "hi", "token": token}},
            headers=hub["admin"],
        )
        assert written.status_code == 200, written.text

    sealer = client.app.state.sealer  # the key the app sealed the secrets with

    async def payload(conn):
        row = (await conn.execute(_plugin_row(conn, "sandbox"))).one()
        return await backend_payload(conn, sealer, "https://hub.test", "evp_x", row)

    result = _run(hub_db, payload)
    instance = result["config"]["instance"]
    assert instance.get("token") == "sandbox-only-secret"
    assert "beta-only-secret" not in str(result)


def test_plugin_backend_status_shows_for_an_admin(client, hub, hub_db):
    report = client.post(
        "/v1/plugin-host/status",
        json=[
            {
                "plugin": "sandbox",
                "version": "1.0.0",
                "state": "running",
                "isolation": "uid",
                "pid": 42,
                "restarts": 0,
                "error": None,
            }
        ],
        headers={"Authorization": f"Bearer {HOST_TOKEN}"},
    )
    assert report.status_code == 204, report.text

    seen = client.get("/v1/plugins/sandbox/backend", headers=hub["admin"])
    assert seen.status_code == 200, seen.text
    body = seen.json()
    assert (body["state"], body["isolation"], body["pid"]) == ("running", "uid", 42)
    assert client.get("/v1/plugins/sandbox/backend", headers=hub["owner"]).status_code == 403  # not a hub admin


def test_plugin_host_routes_need_the_shared_secret(client):
    assert client.get("/v1/plugin-host/plugins").status_code == 401  # no secret
    assert client.get("/v1/plugin-host/plugins", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/v1/plugin-host/plugins", headers={"Authorization": f"Bearer {HOST_TOKEN}"})
    assert ok.status_code == 200, ok.text
