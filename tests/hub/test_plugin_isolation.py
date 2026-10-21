"""The secret boundary of isolated plugins on the hub side (criterion a4 of hub-agent-plugins):

- a plugin's backend token carries only what its installed version asks for and the projects granted
  (``plugin_tokens.issue_token``, ``backend_payload``); the hub mints one only when the plugin host is about to start
  a backend, so pulling the same plugins again changes nothing, and minting revokes the plugin's older tokens;
  removing a plugin or the hub turning it off revokes its tokens at once;
- the ``initialize`` payload of one plugin carries only that plugin's config, its secrets decrypted here where the key
  is, and never another plugin's secret;
- the routes the host calls are outside /v1, which is all the reverse proxy sends to the api, and take only the
  shared secret, which may not be the example value and never shows in a log;
- the api may ask a backend for ping and the extension points' methods only, never ``initialize`` or ``shutdown``;
- a crash or a hang of a backend makes the plugin host answer an error within the timeout (``plugin_host_service``),
  and the api's client to the host answers an error within the timeout when the host is unreachable;
- the plugin host marks itself not dumpable at start, and reports each backend's state, which a hub admin reads at
  ``GET /v1/plugins/{name}/backend``.

The DB checks need the hub (they skip without EVO_HUB_TEST_DSN); the host and client checks run a real backend
subprocess and need no DB.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets

import pytest

from tests.hub import live, pg
from tests.plugins.backends import bundle, init, manifest

EXAMPLE_TOKEN = "dev-only-plugin-host-secret-change-me-000000"  # what deploy/hub/.env.example once held


# The host and client checks (no DB)


def _host_app(tmp_path, token: str):
    from fastapi.testclient import TestClient

    from evo_agents.hub.plugin_host_service import create_host_app
    from evo_agents.plugins.host import Host

    host = Host(tmp_path, allow_shared_user=True)
    host.place(manifest("faulty"), bundle("faulty"), init("faulty"))
    return TestClient(create_host_app(host, token)), host


def _call(tool: str) -> dict:
    return {"tool": tool, "arguments": {}, "caller": {"project": "p"}}


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


def test_plugin_isolation_host_lets_the_api_call_no_lifecycle_method(tmp_path):
    client, host = _host_app(tmp_path, "shared-secret")
    auth = {"Authorization": "Bearer shared-secret"}
    for method in ("initialize", "shutdown", "no-such-method"):
        refused = client.post("/v1/host/plugins/faulty/call", json={"method": method, "params": {}}, headers=auth)
        assert refused.status_code == 403, (method, refused.text)
    assert client.post("/v1/host/plugins/faulty/call", json={"method": "ping"}, headers=auth).status_code == 200
    asyncio.run(host.stop_all())


def test_plugin_isolation_host_without_a_secret_runs_no_backend(tmp_path):
    client, host = _host_app(tmp_path, None)
    assert client.get("/health/live").status_code == 200
    assert client.post("/v1/host/plugins/faulty/call", json={"method": "ping"}).status_code == 503
    assert client.get("/v1/host/backends", headers={"Authorization": "Bearer "}).status_code == 503
    asyncio.run(host.stop_all())


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


def test_plugin_isolation_client_keeps_a_failed_start_out_of_what_members_read(tmp_path):
    """The review of PR #72: the host's 503 says why a backend is down, a failed start's text included (a path, a
    line of the plugin's stderr), and the api passes the client's message on to a project member's route, tool or
    delivery. The client now raises a fixed message; a hub admin reads the cause in the backend's status."""
    import httpx
    from fastapi.testclient import TestClient

    from evo_agents.hub.plugin_host_service import create_host_app
    from evo_agents.hub.server.plugin_host import PluginHostClient, PluginHostUnavailable
    from evo_agents.plugins.host import BackendUnavailable

    cause = "plugin sample could not start: PlacementError: /plugins/envs/sample/1.0.0; stderr: evo-sibling-secret"

    class DownHost:
        async def call(self, *args, **kwargs):
            raise BackendUnavailable(cause)

        def status(self) -> list[dict]:
            return []

    token = "plugin-host-" + secrets.token_urlsafe(32)
    host_app = TestClient(create_host_app(DownHost(), token))
    said = host_app.post(
        "/v1/host/plugins/sample/call", json={"method": "ping"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert said.status_code == 503 and cause in said.text  # the host tells the api, which is the hub's own

    async def call() -> str:
        client = PluginHostClient("http://plugins:8090", token)
        client._client = httpx.AsyncClient(
            base_url=client.url, transport=httpx.MockTransport(lambda request: httpx.Response(503, json=said.json()))
        )
        with pytest.raises(PluginHostUnavailable) as raised:
            await client.call("sample", "ping", {}, timeout=1.0)
        await client.aclose()
        return str(raised.value)

    message = asyncio.run(call())
    assert "not running now" in message and "could not start" not in message and "evo-sibling-secret" not in message


def _host_args(**changes) -> argparse.Namespace:
    values = {"hub_url": "http://api:8080", "data_dir": None, "host": "127.0.0.1", "port": None}
    return argparse.Namespace(**{**values, **changes})


def test_plugin_isolation_host_refuses_the_example_secret_and_never_logs_its_own(monkeypatch, tmp_path):
    from evo_agents.hub.config import ConfigError, load_config
    from evo_agents.hub.log import scrub
    from evo_agents.hub.plugin_host_service import load_host_config

    for refused in (EXAMPLE_TOKEN, "too-short"):
        monkeypatch.setenv("EVO_HUB_PLUGIN_HOST_TOKEN", refused)
        with pytest.raises(ValueError, match="EVO_HUB_PLUGIN_HOST_TOKEN"):
            load_host_config(_host_args())
        with pytest.raises(ConfigError, match="EVO_HUB_PLUGIN_HOST_TOKEN"):
            load_config({"EVO_HUB_DSN": "postgresql://hub@db/hub", "EVO_HUB_PLUGIN_HOST_TOKEN": refused})
    token = "plugin-host-" + secrets.token_urlsafe(32)
    monkeypatch.setenv("EVO_HUB_PLUGIN_HOST_TOKEN", token)
    config = load_host_config(_host_args(data_dir=str(tmp_path)))
    assert config.token == token and not config.allow_shared_user
    assert token not in scrub(f"Authorization: Bearer {token}")
    monkeypatch.delenv("EVO_HUB_PLUGIN_HOST_TOKEN")
    assert load_host_config(_host_args()).token is None  # no secret: the host runs, with no backend


def test_plugin_isolation_host_marks_itself_not_dumpable_at_start(monkeypatch, tmp_path):
    import uvicorn

    from evo_agents.hub import plugin_host_service

    marked = []
    monkeypatch.setattr(plugin_host_service, "set_not_dumpable", lambda: marked.append(True) or True)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)
    config = plugin_host_service.PluginHostConfig("http://api:8080", None, tmp_path, "127.0.0.1", 8090)
    assert plugin_host_service.run_service(config) == 0
    assert marked == [True]


# The DB checks

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select, update  # noqa: E402

import tests.hub.test_plugin_projects as projects  # noqa: E402
from evo_agents.hub import tables  # noqa: E402
from evo_agents.hub.server.app import create_app  # noqa: E402
from evo_agents.hub.server.sealing import KEY_BYTES  # noqa: E402
from tests.hub.test_plugin_projects import ASKED, PROJECT, install, prepare  # noqa: E402

HOST_TOKEN = "plugin-host-shared-" + secrets.token_hex(16)
HOST = {"Authorization": f"Bearer {HOST_TOKEN}"}
BACKEND = {"runtime": "python", "entry": "backend/main.py"}


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


def _engine(hub_db):
    import psycopg
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    return create_async_engine(
        "postgresql+psycopg://", async_creator=lambda: psycopg.AsyncConnection.connect(hub_db.dsn), poolclass=NullPool
    )


def _run(hub_db, fn):
    async def go():
        engine = _engine(hub_db)
        try:
            async with engine.begin() as conn:
                return await fn(conn)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _plugin_row(name: str):
    p, v = tables.plugins, tables.plugin_versions
    return (
        select(p.c.id, p.c.name, p.c.installed_version, v.c.manifest)
        .join_from(p, v, (v.c.plugin_id == p.c.id) & (v.c.version == p.c.installed_version))
        .where(p.c.name == name)
    )


def _give_backend(hub_db, name: str = "sandbox") -> None:
    """The installed version of ``name`` carries a backend, so the host runs it."""
    v, p = tables.plugin_versions, tables.plugins
    with live.engine(hub_db).begin() as conn:
        query = select(v.c.plugin_id, v.c.version, v.c.manifest).join(p, p.c.id == v.c.plugin_id)
        rows = conn.execute(query.where(p.c.name == name)).all()
        for row in rows:
            changed = update(v).where(v.c.plugin_id == row.plugin_id, v.c.version == row.version)
            conn.execute(changed.values(manifest={**row.manifest, "backend": BACKEND}))


def _live(hub_db, *tokens: str) -> list[int | None]:
    from evo_agents.hub.server.plugin_tokens import plugin_of_token

    async def read(conn):
        return [await plugin_of_token(conn, token) for token in tokens]

    return _run(hub_db, read)


def _token_count(hub_db) -> int:
    return live.sql(hub_db, select(func.count()).select_from(tables.plugin_tokens))[0][0]


def test_plugin_token_isolation_carries_only_granted_permissions(client, hub, hub_db):
    from evo_agents.hub.server.plugin_tokens import backend_payload, issue_token

    projects.enabled(client, hub["owner"], ASKED)  # grant exactly what sandbox asks for

    async def mint(conn):
        row = (await conn.execute(_plugin_row("sandbox"))).one()
        first = await issue_token(conn, row.id)
        second = await issue_token(conn, row.id)
        return first, second, await backend_payload(conn, None, "https://hub.test", row)

    first, second, payload = _run(hub_db, mint)
    assert payload["grants"] == {PROJECT: ASKED}
    assert payload["permissions"] == ASKED
    assert payload["plugin"] == {"id": "sandbox", "version": "1.0.0"}
    assert "token" not in payload["hub"]  # the host adds the one it gets at the backend's start
    old, new = _live(hub_db, first, second)
    assert old is None and new is not None  # minting again revoked the older token


def test_plugin_token_isolation_pulling_mints_nothing_and_each_start_mints_one(client, hub, hub_db):
    _give_backend(hub_db)
    pulled = [client.get("/internal/plugin-host/plugins", headers=HOST) for _ in range(2)]
    assert all(response.status_code == 200 for response in pulled), pulled[0].text
    first, second = (response.json() for response in pulled)
    assert [item["name"] for item in first] == ["sandbox"]
    assert first == second, "a pull again would restart every backend"
    assert "token" not in first[0]["init"]["hub"] and _token_count(hub_db) == 0

    started = [client.post("/internal/plugin-host/plugins/sandbox/token", headers=HOST) for _ in range(2)]
    assert all(response.status_code == 200 for response in started), started[0].text
    tokens = [response.json()["token"] for response in started]
    assert all(token.startswith("evp_") for token in tokens)
    assert _live(hub_db, *tokens)[0] is None and _live(hub_db, tokens[1])[0] is not None
    assert client.post("/internal/plugin-host/plugins/nope/token", headers=HOST).status_code == 404


def test_plugin_token_isolation_remove_and_turn_off_revoke_it_at_once(client, hub, hub_db):
    from evo_agents.hub.server.plugin_store import disable_incompatible

    _give_backend(hub_db)
    token = client.post("/internal/plugin-host/plugins/sandbox/token", headers=HOST).json()["token"]
    removed = client.post("/v1/plugins/sandbox/remove", headers=hub["admin"])
    assert removed.status_code == 200, removed.text
    assert _live(hub_db, token) == [None]
    assert client.post("/internal/plugin-host/plugins/sandbox/token", headers=HOST).status_code == 404

    admin = select(tables.users.c.id).where(tables.users.c.login == live.ADMIN).scalar_subquery()
    again = {"installed_version": "1.0.0", "installed_by": admin, "installed_at": func.now()}  # then turned off
    live.sql(hub_db, update(tables.plugins).where(tables.plugins.c.name == "sandbox").values(again))
    token = client.post("/internal/plugin-host/plugins/sandbox/token", headers=HOST).json()["token"]

    async def turn_off():
        engine = _engine(hub_db)
        try:
            return await disable_incompatible(engine, frozenset({live.ADMIN}), "0.1.0")
        finally:
            await engine.dispose()

    assert asyncio.run(turn_off()) == ["sandbox"]
    assert _live(hub_db, token) == [None]


def test_plugin_token_isolation_a_token_that_outlived_its_plugin_opens_nothing(client, hub, hub_db):
    """The review's second L3: a token is live only while its plugin is installed and on, whatever its own row says,
    so one minted in a race with a remove opens nothing."""
    _give_backend(hub_db)
    token = client.post("/internal/plugin-host/plugins/sandbox/token", headers=HOST).json()["token"]
    p = tables.plugins
    plugin = p.c.name == "sandbox"
    columns = ("installed_version", "installed_by", "installed_at")
    (row,) = live.sql(hub_db, select(*(p.c[name] for name in columns)).where(plugin))
    installed = dict(zip(columns, row, strict=True))
    plugin_id = _live(hub_db, token)[0]
    assert plugin_id is not None

    removed = {"installed_version": None, "installed_by": None, "installed_at": None}  # its token not revoked
    live.sql(hub_db, update(p).where(plugin).values(removed))
    assert _live(hub_db, token) == [None]
    live.sql(hub_db, update(p).where(plugin).values(installed))
    assert _live(hub_db, token) == [plugin_id]
    live.sql(hub_db, update(tables.plugins).where(plugin).values(disabled_reason="off", disabled_at=func.now()))
    assert _live(hub_db, token) == [None]


def test_plugin_token_isolation_minting_waits_for_a_remove_under_way(client, hub, hub_db):
    """The review's second L3: minting read the plugin without a lock, so a remove committing between that read and
    the token's insert revoked the older tokens and left the new one live. Minting now waits on the plugin's row and
    finds no installed plugin once the remove commits."""
    from types import SimpleNamespace

    from evo_agents.hub.server.plugin_store import find, set_removed

    _give_backend(hub_db)

    async def race():
        engine = _engine(hub_db)
        try:
            async with engine.begin() as conn:
                row = await find(conn, "sandbox", lock=True)
                await set_removed(conn, SimpleNamespace(user_id=None, token_id=None), row)
                post = client.post
                minting = asyncio.ensure_future(
                    asyncio.to_thread(post, "/internal/plugin-host/plugins/sandbox/token", headers=HOST)
                )
                await asyncio.sleep(1.0)
                waited = not minting.done()
            return waited, await minting
        finally:
            await engine.dispose()

    waited, minted = asyncio.run(race())
    assert waited, "minting did not wait for the remove under way"
    assert minted.status_code == 404, minted.text
    pt = tables.plugin_tokens
    assert live.sql(hub_db, select(func.count()).select_from(pt).where(pt.c.revoked_at.is_(None)))[0][0] == 0


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
        row = (await conn.execute(_plugin_row("sandbox"))).one()
        return await backend_payload(conn, sealer, "https://hub.test", row)

    result = _run(hub_db, payload)
    assert result["config"]["instance"].get("token") == "sandbox-only-secret"
    assert "beta-only-secret" not in str(result)


def test_plugin_token_isolation_a_disabled_plugin_grants_nothing(client, hub, hub_db):
    from evo_agents.hub.server.plugin_tokens import backend_payload

    projects.enabled(client, hub["owner"], ASKED)
    projects.turn_off(hub_db, "incompatible")  # the hub turned it off

    async def payload(conn):
        row = (await conn.execute(_plugin_row("sandbox"))).one()
        return await backend_payload(conn, None, "https://hub.test", row)

    assert _run(hub_db, payload)["grants"] == {}


def test_plugin_backend_status_shows_for_an_admin(client, hub, hub_db):
    report = client.post(
        "/internal/plugin-host/status",
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
        headers=HOST,
    )
    assert report.status_code == 204, report.text

    seen = client.get("/v1/plugins/sandbox/backend", headers=hub["admin"])
    assert seen.status_code == 200, seen.text
    body = seen.json()
    assert (body["state"], body["isolation"], body["pid"]) == ("running", "uid", 42)
    assert client.get("/v1/plugins/sandbox/backend", headers=hub["owner"]).status_code == 403  # not a hub admin


def test_plugin_host_routes_need_the_shared_secret_and_live_outside_v1(client):
    from evo_agents.hub.plugin_host_service import HUB_ROUTES
    from evo_agents.hub.server.plugin_host import HOST_ROUTES

    assert HUB_ROUTES == HOST_ROUTES and not HOST_ROUTES.startswith("/v1")  # the proxy forwards /v1 and /mcp only
    assert client.get("/internal/plugin-host/plugins").status_code == 401  # no secret
    assert client.get("/internal/plugin-host/plugins", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/internal/plugin-host/plugins", headers=HOST).status_code == 200
    assert client.get("/v1/plugin-host/plugins", headers=HOST).status_code in (401, 404)  # gone from the public API
    paths = client.get("/v1/openapi.json").json()["paths"]
    assert not [path for path in paths if "plugin-host" in path]
