"""The model gateway (``evo_agents.hub.server.gateway_client`` and ``gateway_sync``), plan model-providers, criteria a8
and a4: the hub is the source of truth of the gateway's connections, made at start, after each write of a provider
account and on a timer; /v1/health reports it; its credentials and the providers' keys reach it and nothing else.

``FakeGateway`` stands for OmniRoute 3.8.52 as the hub uses it, read from its source and checked against the pinned
image (test_gateway_image.py): the dashboard password signs in and sets the cookie auth_token, which every management
route needs; a new connection is inactive; deleting a provider node deletes its connections. No Node runs here."""

import asyncio
import json
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import update

from evo_agents.hub import model_catalog, tables
from evo_agents.hub.config import load_config
from evo_agents.hub.server import gateway_sync
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.gateway_client import (
    GATEWAY_DIGEST,
    GATEWAY_IMAGE,
    GatewayClient,
    GatewayError,
    GatewayOff,
    derive,
)
from tests.hub.live import sql
from tests.hub.test_compose import compose
from tests.hub.test_provider_accounts import (
    KEY,
    MODELS,
    PROJECT,
    account,
    nowhere,
    path,
    resolve,
    sample,
    stored,
    written,
)
from tests.hub.test_provider_accounts import hub as hub  # noqa: F401  the fixture
from tests.hub.test_runs import add_worker

SECRET = secrets.token_urlsafe(32)
URL = "http://gateway.test:20128"
AGENT_BASE = f"/v1/projects/{PROJECT}/agents/auditor/provider-accounts"


ZONE = timezone(timedelta(hours=7))  # an offset the client turns into Z
ZOD_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")  # zod's datetime(): no offset


def _refused_key(body: dict) -> list[dict]:
    """What OmniRoute 3.8.52's createKeySchema refuses in ``body``, as its 400 lists it."""
    refused = []
    ends = body.get("expiresAt")
    if ends is not None and not ZOD_DATETIME.fullmatch(ends):
        refused.append({"field": "expiresAt", "message": "Invalid ISO datetime"})
    for connection in body.get("allowedConnections") or ():
        try:
            uuid.UUID(connection)
        except ValueError:
            refused.append({"field": "allowedConnections", "message": "Invalid uuid"})
    return refused


class FakeGateway:
    """OmniRoute's management API, in memory. ``down`` makes every call fail; ``lose`` empties it, as a lost volume."""

    def __init__(self, secret: str = SECRET) -> None:
        self.password = derive(secret, "password")
        self.sessions: set[str] = set()
        self.connections: dict[str, dict] = {}
        self.nodes: dict[str, dict] = {}
        self.keys: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.down = False
        self.fail_with: int | None = None  # every management write answers this, echoing what it got

    def lose(self) -> None:
        self.sessions.clear()
        self.connections.clear()
        self.nodes.clear()
        self.keys.clear()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("gateway down", request=request)
        route = (request.method, request.url.path)
        if route == ("GET", "/api/health"):
            return httpx.Response(200, json={"status": "ok"})
        if route == ("POST", "/api/auth/login"):
            if _body(request).get("password") == self.password:
                session = secrets.token_hex(16)
                self.sessions.add(session)
                return httpx.Response(
                    200, json={"success": True}, headers={"set-cookie": f"auth_token={session}; Path=/"}
                )
            return httpx.Response(401, json={"error": "Invalid password"})
        if _cookie(request) not in self.sessions:
            return httpx.Response(401, json={"error": "Authentication required"})
        if self.fail_with and request.method != "GET":
            return httpx.Response(self.fail_with, text=f"failed on {request.content.decode()}")
        return self._manage(request)

    def _manage(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")[1:]  # after api
        method, body = request.method, _body(request)
        if parts[0] == "providers":
            return self._providers(method, parts[1:], body)
        if parts[0] == "provider-nodes":
            return self._nodes(method, parts[1:], body)
        if parts[0] == "keys":
            return self._keys(method, parts[1:], body)
        if parts == ["usage", "analytics"]:
            key_id = request.url.params["apiKeyIds"]
            used = self.keys[key_id]["used"]
            return httpx.Response(200, json={"summary": used, "byApiKey": []})
        return httpx.Response(404, json={"error": "not found"})

    def _providers(self, method: str, rest: list[str], body: dict) -> httpx.Response:
        if method == "GET":
            shown = [{**c, "apiKey": "sk-****"} for c in self.connections.values()]
            return httpx.Response(200, json={"connections": shown, "total": len(shown)})
        if method == "POST":
            found = {"id": str(uuid.uuid4()), "isActive": False, "providerSpecificData": {}, **body}
            self.connections[found["id"]] = found
            return httpx.Response(201, json={"connection": {**found, "apiKey": "sk-****"}})
        if rest[0] not in self.connections:
            return httpx.Response(404, json={"error": "Connection not found"})
        if method == "PUT":
            self.connections[rest[0]].update(body)
            return httpx.Response(200, json={"connection": {**self.connections[rest[0]], "apiKey": "sk-****"}})
        del self.connections[rest[0]]
        return httpx.Response(200, json={"message": "Connection deleted successfully"})

    def _nodes(self, method: str, rest: list[str], body: dict) -> httpx.Response:
        if method == "GET":
            return httpx.Response(200, json={"nodes": list(self.nodes.values())})
        if method == "POST":
            found = {"id": f"{body['type']}-{body['apiType']}-{uuid.uuid4()}", **body}
            self.nodes[found["id"]] = found
            return httpx.Response(201, json={"node": found})
        self.nodes.pop(rest[0])
        for gone in [c["id"] for c in self.connections.values() if c["provider"] == rest[0]]:
            del self.connections[gone]
        return httpx.Response(200, json={"success": True})

    def _keys(self, method: str, rest: list[str], body: dict) -> httpx.Response:
        if method == "POST":
            if refused := _refused_key(body):
                return httpx.Response(400, json={"error": {"message": "Invalid request", "details": refused}})
            value = "sk-" + secrets.token_hex(24)
            used = {"totalRequests": 3, "promptTokens": 120, "completionTokens": 45, "totalCost": 0.0125}
            found = {"id": str(uuid.uuid4()), "revokedAt": None, **body, "value": value, "used": used}
            self.keys[found["id"]] = found
            return httpx.Response(201, json={**_shown(found), "key": value})
        if method == "GET":
            return httpx.Response(200, json={"keys": [_shown(k) for k in self.keys.values()], "total": len(self.keys)})
        del self.keys[rest[0]]
        return httpx.Response(200, json={"message": "Key deleted successfully"})

    def active(self) -> dict[str, bool]:
        """The connections by name, with whether each is active."""
        return {c["name"]: c["isActive"] for c in self.connections.values()}

    def sent_keys(self) -> list[str]:
        return [c["apiKey"] for c in self.connections.values()]


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content) if request.content else {}


def _cookie(request: httpx.Request) -> str | None:
    header = request.headers.get("cookie", "")
    return dict(part.strip().split("=", 1) for part in header.split(";") if "=" in part).get("auth_token")


def _shown(key: dict) -> dict:
    return {name: value for name, value in key.items() if name not in ("value", "used")}


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


def _app(hub_db, tmp_path, github, gateway: FakeGateway | None, **changes):
    if gateway is not None:
        changes |= {"gateway_url": URL, "gateway_secret": SECRET}
    app = create_app(live.hub_config(hub_db, tmp_path, github, secrets_key=KEY, **changes))
    app.state.provider_transport = httpx.MockTransport(lambda request: httpx.Response(200, json=MODELS))
    app.state.provider_resolver = resolve
    app.state.gateway_transport = httpx.MockTransport(gateway) if gateway is not None else None
    app.state.gateway_interval = 3600  # no round on a timer unless a test asks
    return app


@pytest.fixture
def client(hub_db, tmp_path, github, gateway):
    with TestClient(_app(hub_db, tmp_path, github, gateway), base_url="https://hub.test") as client:
        yield client


def settled(client) -> None:
    client.portal.call(client.app.state.gateway_sync.settled)


def poked(client) -> None:
    client.app.state.gateway_sync.poke()
    settled(client)


# Reconcile: the hub's provider accounts become the gateway's connections


def test_gateway_reconcile_makes_a_connection_of_each_usable_account_and_follows_every_write(
    client, hub, gateway, hub_db
):
    key, rotated, custom_key, agent_key = sample(), sample(), sample(), sample()
    made = written(client, hub["owner"], "or", account(key))
    url = "https://llm.example.com/v1"
    written(client, hub["owner"], "custom", account(custom_key, provider="custom-openai", base_url=url))
    add_worker(client, hub["owner"], "box")
    agent = {"provider": "openrouter", "value": agent_key, "workers": ["box"], "models": ["a/b"]}
    assert client.put(f"{AGENT_BASE}/agent-or", json=agent, headers=hub["owner"]).status_code == 200
    settled(client)
    by_key = {c["apiKey"]: c for c in gateway.connections.values()}
    assert set(by_key) == {key, custom_key, agent_key} and all(c["isActive"] for c in by_key.values())
    entry = model_catalog.find("openrouter")
    assert (
        by_key[key]["provider"] == "openrouter" and by_key[key]["providerSpecificData"]["baseUrl"] == made["base_url"]
    )
    assert made["base_url"] == entry["base_url"]
    (node,) = gateway.nodes.values()
    custom_id = stored(hub_db, "custom")[0]
    assert node["name"] == node["prefix"] == f"evo-{custom_id}" and node["baseUrl"] == url
    assert (node["type"], node["apiType"]) == ("openai-compatible", "chat")
    assert by_key[custom_key]["provider"] == node["id"] and "baseUrl" not in by_key[custom_key]["providerSpecificData"]
    first = by_key[key]["id"]

    assert client.patch(path("or"), json={"paused": True}, headers=hub["owner"]).status_code == 200
    settled(client)
    assert gateway.connections[first]["isActive"] is False
    assert client.patch(path("or"), json={"paused": False, "value": rotated}, headers=hub["owner"]).status_code == 200
    settled(client)
    assert gateway.connections[first]["isActive"] is True and gateway.connections[first]["apiKey"] == rotated, (
        "a new key updates the connection in place, so it keeps its id"
    )
    assert client.patch(f"{AGENT_BASE}/agent-or", json={"paused": True}, headers=hub["owner"]).status_code == 200
    settled(client)
    assert [c["isActive"] for c in gateway.connections.values() if c["apiKey"] == agent_key] == [False]

    for gone in (path("or"), path("custom"), f"{AGENT_BASE}/agent-or"):
        assert client.delete(gone, headers=hub["owner"]).status_code == 204
    settled(client)
    assert gateway.connections == {} and gateway.nodes == {}


def test_gateway_reconcile_turns_off_an_expired_account_and_deletes_what_the_hub_does_not_know(
    client, hub, gateway, hub_db
):
    key = sample()
    written(client, hub["owner"], "or", account(key))
    settled(client)
    gateway.connections["stranger"] = {"id": "stranger", "provider": "openai", "name": "hand-made", "isActive": True}
    gateway.nodes["openai-compatible-chat-x"] = {"id": "openai-compatible-chat-x", "name": "x", "prefix": "x"}
    s = tables.secrets
    sql(hub_db, update(s).where(s.c.name == "or").values(expires_at=s.c.created_at))
    poked(client)
    assert list(gateway.active().values()) == [False] and "stranger" not in gateway.connections
    assert gateway.nodes == {}


def test_gateway_reconcile_rebuilds_a_gateway_that_lost_its_volume_and_signs_in_again(client, hub, gateway):
    key = sample()
    written(client, hub["owner"], "or", account(key))
    settled(client)
    logins = sum(r.url.path == "/api/auth/login" for r in gateway.requests)
    gateway.lose()
    poked(client)
    assert gateway.sent_keys() == [key] and list(gateway.active().values()) == [True]
    assert sum(r.url.path == "/api/auth/login" for r in gateway.requests) == logins + 1, "a 401 signs in once more"


def test_gateway_reconcile_runs_at_start_and_on_its_timer(hub_db, tmp_path, github, hub, client, gateway):
    key = sample()
    written(client, hub["owner"], "or", account(key))
    settled(client)
    later = FakeGateway()
    app = _app(hub_db, tmp_path, github, later)
    app.state.gateway_interval = 0.05
    with TestClient(app, base_url="https://hub.test") as other:
        other.portal.call(other.app.state.gateway_sync.settled)
        assert later.sent_keys() == [key], "the start made it"
        later.lose()
        for _ in range(100):
            if later.sent_keys():
                break
            other.portal.call(_pause)
        assert later.sent_keys() == [key], "the timer made it again"


async def _pause() -> None:
    await asyncio.sleep(0.05)


def test_gateway_reconcile_survives_a_gateway_that_fails_and_tries_again(client, hub, gateway, caplog):
    key = sample()
    gateway.down = True
    written(client, hub["owner"], "or", account(key))
    settled(client)
    assert client.app.state.gateway_sync.last_error and gateway.connections == {}
    gateway.down = False
    poked(client)
    assert client.app.state.gateway_sync.last_error is None and gateway.sent_keys() == [key]


def test_gateway_reconcile_is_off_without_the_secret_and_the_hub_runs_as_before(hub_db, tmp_path, github, hub):
    app = _app(hub_db, tmp_path, github, None)
    with TestClient(app, base_url="https://hub.test") as client:
        assert app.state.gateway is None and app.state.gateway_sync is None
        assert client.get(path(), headers=hub["owner"]).status_code == 200
        with pytest.raises(GatewayOff, match="not configured.*EVO_HUB_GATEWAY_SECRET"):
            gateway_sync.client_of(app)
    assert load_config({"EVO_HUB_DSN": "postgresql://h@db/h"}).gateway_missing() == ["EVO_HUB_GATEWAY_SECRET"]
    with pytest.raises(Exception, match="EVO_HUB_GATEWAY_SECRET must be at least 32"):
        load_config({"EVO_HUB_DSN": "postgresql://h@db/h", "EVO_HUB_GATEWAY_SECRET": "short"})


# Health


def test_gateway_health_reports_ok_then_error_and_fails_the_check(client, gateway):
    found = client.get("/v1/health")
    assert found.status_code == 200 and found.json()["gateway"] == "ok"
    gateway.down = True
    found = client.get("/v1/health")
    assert found.status_code == 503 and found.json()["gateway"] == "error" and found.json()["failed"] == ["gateway"]


def test_gateway_health_reports_off_without_the_secret_and_stays_healthy(hub_db, tmp_path, github):
    with TestClient(_app(hub_db, tmp_path, github, None)) as client:
        found = client.get("/v1/health")
    assert found.status_code == 200 and found.json()["gateway"] == "off" and found.json()["failed"] == []


# Secrecy


def test_gateway_secrecy_keys_and_credentials_reach_the_gateway_alone(client, hub, gateway, hub_db, caplog):
    caplog.set_level(logging.DEBUG)
    key = sample()
    written(client, hub["owner"], "or", account(key))
    settled(client)
    gateway.fail_with = 500  # what it answers echoes the key, as some servers do
    client.patch(path("or"), json={"value": (rotated := sample())}, headers=hub["owner"])
    settled(client)
    error = client.app.state.gateway_sync.last_error
    assert error == "the model gateway failed to update a connection (HTTP 500)"
    password = derive(SECRET, "password")
    cookies = {_cookie(r) for r in gateway.requests} - {None}
    assert cookies and all(r.url.host == "gateway.test" for r in gateway.requests)
    nowhere(client, hub_db, hub["owner"], caplog, key, rotated, SECRET, password, *cookies)
    health = client.get("/v1/health").text
    assert all(value not in health for value in (SECRET, password, URL))
    assert SECRET not in repr(client.app.state.config) and password not in repr(client.app.state.gateway)


def test_gateway_secrecy_the_client_makes_no_log_keys_and_names_no_body_in_an_error(client, gateway):
    gateway_client = client.app.state.gateway

    async def round_trip():
        connection = await gateway_client.create_connection("openrouter", "c", sample(), "https://openrouter.ai/api/v1")
        issued = await gateway_client.create_key("run-1", [connection.id], datetime(2027, 1, 1, 7, tzinfo=ZONE))
        listed = await gateway_client.keys()
        usage = await gateway_client.key_usage(issued.id)
        await gateway_client.revoke_key(issued.id)
        return connection, issued, listed, usage, await gateway_client.keys()

    connection, issued, listed, usage, after = client.portal.call(round_trip)
    assert (
        issued.no_log
        and issued.allowed_connections == (connection.id,)
        and issued.expires_at == "2027-01-01T00:00:00.000Z"
    )
    assert issued.value.get_secret_value().startswith("sk-") and issued.value.get_secret_value() not in repr(issued)
    assert [k.id for k in listed] == [issued.id] and all(k.no_log for k in listed) and after == []
    assert (usage.requests, usage.prompt_tokens, usage.completion_tokens, usage.cost_usd) == (3, 120, 45, 0.0125)
    gateway.fail_with = 400

    async def refused():
        await gateway_client.create_connection("openrouter", "c", "sk-echoed-" + "x" * 20)

    with pytest.raises(GatewayError) as caught:
        client.portal.call(refused)
    assert "echoed" not in str(caught.value) and caught.value.status == 400


def test_gateway_secrecy_a_wrong_secret_does_not_sign_in_and_says_why(client):
    other = GatewayClient(URL, secrets.token_urlsafe(32), httpx.MockTransport(FakeGateway()))

    async def listed():
        try:
            return await other.connections()
        finally:
            await other.aclose()

    with pytest.raises(GatewayError, match="sign in.*another EVO_HUB_GATEWAY_SECRET.*HTTP 401"):
        client.portal.call(listed)


# The compose file runs the image this client was written against


def test_gateway_compose_pins_the_image_the_client_knows():
    assert compose()["services"]["gateway"]["image"] == GATEWAY_IMAGE
    assert GATEWAY_IMAGE.endswith("@" + GATEWAY_DIGEST) and f":{model_catalog.GATEWAY_VERSION}@" in GATEWAY_IMAGE
