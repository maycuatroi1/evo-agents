"""Runs on a provider account's model through the model gateway (``evo_agents.hub.gateway_runs``,
``server.gateway_accounts``, ``gateway_leases`` and ``gateway_proxy``), plan model-providers, criteria a3, a4 and a5:
a dispatch of ``<alias>/<model>`` needs an account the run may use and says why when there is none; only a daemon of
GATEWAY_RUN_AGENT or later claims such a run; its lease carries the hub's route, a run token, the model and the format,
never a key; the hub's route relays its calls with the run's key of the gateway, for the models of its provider alone,
until the run leaves the held states; the gateway's usage reaches the run, and the account's figures and listings.

The gateway is ``FakeGateway`` of test_gateway.py, with its client endpoints (``Relaying``)."""

import io
import json
import logging

import httpx
import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from evo_agents.hub import tables
from evo_agents.hub.credentials import Lease
from evo_agents.hub.gateway_runs import GATEWAY_RUN_AGENT
from evo_agents.hub.server.gateway_leases import TOKEN_PREFIX
from tests.hub.live import sql, table_dump
from tests.hub.test_credentials_api import audit_rows, leased, leases_of
from tests.hub.test_gateway import FakeGateway, _app, poked, settled
from tests.hub.test_provider_accounts import PROJECT, account, path, sample, stored, written
from tests.hub.test_provider_accounts import hub as hub  # noqa: F401  the fixture
from tests.hub.test_run_cli import write_credentials
from tests.hub.test_runs import OWNER, add_worker, claim, control, dispatch, moved, report, state_of
from tests.hub.test_secrets_cli import run_cli

MODEL = "openrouter/auto"
NEW_DAEMON = ".".join(str(part) for part in GATEWAY_RUN_AGENT)
STREAM = b'event: message_start\ndata: {"type": "message_start"}\n\nevent: message_stop\ndata: {}\n\n'


class Relaying(FakeGateway):
    """The gateway with its client endpoints: /v1/* takes a key it made and not revoked, answers server-sent events,
    and lists models of two providers."""

    def __init__(self) -> None:
        super().__init__()
        self.relayed: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not request.url.path.startswith("/v1/"):
            return super().__call__(request)
        self.relayed.append(request)
        given = request.headers.get("authorization", "").removeprefix("Bearer ")
        if given not in {key["value"] for key in self.keys.values()}:
            return httpx.Response(401, json={"error": "invalid key"})
        if request.url.path == "/v1/models":
            listed = [{"id": MODEL}, {"id": "anthropic/claude-x"}]
            return httpx.Response(200, json={"object": "list", "data": listed})
        return httpx.Response(200, content=STREAM, headers={"content-type": "text/event-stream"})

    def key_values(self) -> list[str]:
        return [key["value"] for key in self.keys.values()]


@pytest.fixture
def gateway() -> Relaying:
    return Relaying()


@pytest.fixture
def client(hub_db, tmp_path, github, gateway):
    with TestClient(_app(hub_db, tmp_path, github, gateway), base_url="https://hub.test") as client:
        yield client


def provider_account(client, headers, name: str = "or", **extra) -> None:
    """A provider account of openrouter serving ``auto``, bound to the project, with its connection at the gateway."""
    written(client, headers, name, account(sample(), models=["auto"], **extra))
    settled(client)


def new_daemon(client, hub_db, headers, name: str = "mac-mini") -> dict:
    worker = add_worker(client, headers, name)
    workers = tables.workers
    sql(hub_db, update(workers).values(agent_version=NEW_DAEMON).where(workers.c.id == worker["id"]))
    return worker


def gateway_run(client, hub_db, headers, worker: dict) -> int:
    response = dispatch(client, headers, [2], runtime="claude-code", model=MODEL)
    assert response.status_code == 201, response.text
    run_id = response.json()[0]["id"]
    assert claim(client, worker)["id"] == run_id
    return run_id


def gateway_lease(answer: dict) -> dict:
    (found,) = [lease for lease in answer["leases"] if lease["kind"] == "gateway"]
    return found


def relay(client, token: str, model: str = MODEL, route: str = "/v1/messages"):
    body = {"model": model, "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    return client.post(route, json=body, headers={"x-api-key": token, "anthropic-version": "2023-06-01"})


# Dispatch and claim


def test_gateway_run_dispatch_needs_an_account_it_may_use_and_says_why(client, hub, hub_db):
    worker = add_worker(client, hub["owner"], "old-mini")  # evo-agents 0.4.0
    refused = dispatch(client, hub["owner"], [2], runtime="claude-code", model=MODEL)
    assert refused.status_code == 422
    assert "no provider account of openrouter that this run may use" in refused.json()["message"]
    assert "nothing was dispatched" in refused.json()["message"]

    provider_account(client, hub["owner"])
    secret_id = stored(hub_db, "or")[0]
    assert client.patch(path("or"), json={"paused": True}, headers=hub["owner"]).status_code == 200
    paused = dispatch(client, hub["owner"], [2], runtime="claude-code", model=MODEL)
    assert paused.status_code == 422 and f"account {secret_id} is paused" in paused.json()["message"]
    assert client.patch(path("or"), json={"paused": False}, headers=hub["owner"]).status_code == 200

    pinned = dispatch(client, hub["owner"], [2], runtime="claude-code", model=MODEL, worker_id=worker["id"])
    assert pinned.status_code == 409 and NEW_DAEMON in pinned.json()["message"]
    other = dispatch(client, hub["other"], [2], runtime="claude-code", model=MODEL)
    assert other.status_code == 422, "another member's account is not theirs to use"

    # opencode reads <provider>/<model> as its own: without an account of that prefix, it is not a gateway model
    plain = dispatch(client, hub["owner"], [4], runtime="opencode", model="openai/gpt-5")
    assert plain.status_code == 201, plain.text
    assert sql(hub_db, select(tables.runs.c.gateway_provider).where(tables.runs.c.id == plain.json()[0]["id"])) == [
        (None,)
    ]


def test_gateway_run_is_claimed_by_a_new_daemon_alone(client, hub, hub_db):
    provider_account(client, hub["owner"])
    old = add_worker(client, hub["owner"], "old-mini")
    response = dispatch(client, hub["owner"], [2], runtime="claude-code", model=MODEL)
    assert response.status_code == 201, response.text
    run = response.json()[0]
    assert sql(hub_db, select(tables.runs.c.gateway_provider).where(tables.runs.c.id == run["id"])) == [("openrouter",)]
    assert claim(client, old) is None, "a daemon older than GATEWAY_RUN_AGENT would run it on its own sign-in"
    assert control(client, hub["owner"], run["id"], "cancel").status_code == 200
    again = control(client, hub["owner"], run["id"], "rerun")
    assert again.status_code == 201, again.text
    assert sql(hub_db, select(tables.runs.c.gateway_provider).where(tables.runs.c.id == again.json()["id"])) == [
        ("openrouter",)
    ]
    workers = tables.workers
    sql(hub_db, update(workers).values(agent_version=NEW_DAEMON).where(workers.c.id == old["id"]))
    assert claim(client, old)["id"] == again.json()["id"]


def test_gateway_run_models_lists_the_models_of_the_accounts_the_member_may_use(client, hub):
    route = f"/v1/projects/{PROJECT}/runnable-models"
    assert client.get(route, headers=hub["owner"]).json() == []
    provider_account(client, hub["owner"])
    found = client.get(route, headers=hub["owner"])
    assert found.status_code == 200 and found.headers["cache-control"] == "no-store"
    assert found.json() == [
        {"model": MODEL, "prefix": "openrouter", "runtimes": ["claude-code", "opencode", "codex"], "accounts": ["or"]}
    ]
    assert client.get(route, headers=hub["other"]).json() == []
    assert client.patch(path("or"), json={"paused": True}, headers=hub["owner"]).status_code == 200
    assert client.get(route, headers=hub["owner"]).json() == []
    assert client.get(route, headers=hub["stranger"]).status_code == 404


# The lease and the route


def test_gateway_lease_carries_the_route_a_run_token_the_model_and_the_format(client, hub, hub_db, gateway):
    provider_account(client, hub["owner"])
    secret_id = stored(hub_db, "or")[0]
    worker = new_daemon(client, hub_db, hub["owner"])
    run_id = gateway_run(client, hub_db, hub["owner"], worker)
    answer = leased(client, worker, run_id)
    lease = gateway_lease(answer)
    assert (lease["provider"], lease["name"], lease["model"], lease["api_format"]) == (
        "gateway",
        "gateway:openrouter",
        MODEL,
        "anthropic",
    )
    assert lease["base_url"] == "https://hub.test/v1" and lease["value"].startswith(TOKEN_PREFIX)
    read = Lease.from_json(lease)
    assert (read.kind, read.base_url, read.model, read.api_format) == ("gateway", lease["base_url"], MODEL, "anthropic")

    (key,) = gateway.keys.values()
    (connection,) = gateway.connections.values()
    assert key["allowedConnections"] == [connection["id"]] and key["noLog"] is True and key["expiresAt"]
    assert key["value"] != lease["value"] and key["value"] not in json.dumps(answer)
    rows = leases_of(hub_db, run_id)
    assert [(row[1], row[2], row[4]) for row in rows] == [("gateway", None, False), ("gateway", "or", False)]
    assert audit_rows(hub_db, "credential.lease")[-1][0].endswith(f"provider-accounts={secret_id}")

    again = gateway_lease(leased(client, worker, run_id))
    assert again["value"] == lease["value"] and len(gateway.keys) == 1, "the same accounts keep the same key"


def test_gateway_proxy_relays_with_the_runs_key_and_refuses_once_the_run_ends(client, hub, hub_db, gateway):
    provider_account(client, hub["owner"])
    worker = new_daemon(client, hub_db, hub["owner"])
    run_id = gateway_run(client, hub_db, hub["owner"], worker)
    token = gateway_lease(leased(client, worker, run_id))["value"]
    (key,) = gateway.key_values()

    answered = relay(client, token)
    assert answered.status_code == 200, answered.text
    assert answered.content == STREAM and answered.headers["content-type"].startswith("text/event-stream")
    sent = gateway.relayed[-1]
    assert sent.headers["authorization"] == f"Bearer {key}" and "x-api-key" not in sent.headers
    assert token not in sent.content.decode() and sent.headers["anthropic-version"] == "2023-06-01"
    bearer = client.post("/v1/chat/completions", json={"model": MODEL}, headers={"Authorization": f"Bearer {token}"})
    assert bearer.status_code == 200 and gateway.relayed[-1].url.path == "/v1/chat/completions"

    assert relay(client, token, model="anthropic/claude-x").status_code == 403
    assert relay(client, token, model="auto").status_code == 403
    assert relay(client, TOKEN_PREFIX + "forged").status_code == 401
    assert client.post("/v1/messages", json={"model": MODEL}).status_code == 401
    listed = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert listed.status_code == 200 and [item["id"] for item in listed.json()["data"]] == [MODEL]

    moved(client, worker, run_id, "running")
    assert report(client, worker, run_id, "failed", error="verify failed").status_code == 200
    assert state_of(hub_db, run_id) == "failed"
    assert relay(client, token).status_code == 401, "the run left the held states"
    settled(client)
    assert gateway.keys == {}, "the sweep took the key back from the gateway"
    usage = sql(hub_db, select(tables.runs.c.usage).where(tables.runs.c.id == run_id))[0][0]
    assert usage["gateway"]["requests"] == 3 and usage["total_cost_usd"] == pytest.approx(0.0125)
    assert all(row[4] for row in leases_of(hub_db, run_id))


# Figures and listings


def test_provider_figures_count_the_runs_cost_and_holders_of_a_gateway_run(client, hub, hub_db, gateway):
    provider_account(client, hub["owner"])
    worker = new_daemon(client, hub_db, hub["owner"])
    run_id = gateway_run(client, hub_db, hub["owner"], worker)
    leased(client, worker, run_id)
    before = client.get("/v1/accounts/or", headers=hub["owner"]).json()["gateway"]["status"]
    assert before == "unknown", "the gateway saw nothing of the connection it made in this round"
    poked(client)

    (shown,) = [item for item in client.get("/v1/accounts", headers=hub["owner"]).json() if item["name"] == "or"]
    assert shown["env_var"] == "gateway:openrouter" and shown["category"] == "model"
    assert shown["gateway"] == {
        "provider": "openrouter",
        "prefix": "openrouter",
        "base_url_host": "openrouter.ai",
        "api_format": "openai",
        "models": 1,
        "status": "ok",
        "reason": None,
    }
    assert [held["run_id"] for held in shown["held_by"]] == [run_id] and shown["last_leased_at"]
    assert shown["runs_24h"] == shown["runs_7d"] == 1

    moved(client, worker, run_id, "running")
    assert report(client, worker, run_id, "failed", error="verify failed").status_code == 200
    settled(client)
    one = client.get("/v1/accounts/or", headers=hub["owner"]).json()
    assert one["held_by"] == [] and one["runs_7d"] == 1
    assert one["cost_24h_usd"] == pytest.approx(0.0125) and one["cost_7d_usd"] == pytest.approx(0.0125)

    (connection,) = gateway.connections.values()
    connection |= {"testStatus": "error", "errorCode": "rate_limited", "lastError": f"echoes {sample()}"}
    connection["rateLimitedUntil"] = "2999-01-01T00:00:00Z"
    poked(client)
    cooling = client.get("/v1/accounts/or", headers=hub["owner"]).json()
    assert (cooling["state"], cooling["gateway"]["status"], cooling["gateway"]["reason"]) == (
        "cooling",
        "cooling",
        "rate_limited",
    )
    assert "echoes" not in json.dumps(cooling)
    connection["rateLimitedUntil"] = None
    poked(client)
    failing = client.get("/v1/accounts/or", headers=hub["owner"]).json()
    assert (failing["state"], failing["gateway"]["status"]) == ("ready", "failing")


def test_provider_account_cli_lists_and_shows_provider_accounts(client, hub, tmp_path, monkeypatch, capsys):
    from evo_agents.hub import client as hub_client

    provider_account(client, hub["owner"])
    poked(client)
    home = write_credentials(tmp_path / "owner", "https://hub.test", OWNER, hub["owner"]["Authorization"][7:])

    def call(self, method, target, body=None, *, timeout=None):
        response = client.request(method, target, json=body, headers=hub["owner"])
        assert response.status_code < 400, response.text
        return response.json() if response.content else None

    monkeypatch.setattr(hub_client.Hub, "call", call)
    listed = run_cli(monkeypatch, capsys, home, "hub", "account", "list")
    assert listed.code == 0, listed.err
    assert "openrouter (openrouter.ai, openai, 1 models)" in listed.out and "gateway:openrouter" in listed.out
    shown = run_cli(monkeypatch, capsys, home, "hub", "account", "show", "or")
    assert shown.code == 0, shown.err
    assert "Provider account: openrouter (openrouter.ai, openai, 1 models); the gateway: ok." in shown.out
    as_json = run_cli(monkeypatch, capsys, home, "hub", "account", "list", "--json", stdin=io.StringIO())
    assert json.loads(as_json.out)[0]["gateway"]["models"] == 1


# Secrecy


def test_gateway_secrecy_no_key_and_no_token_outside_the_run_and_the_gateway(client, hub, hub_db, gateway, caplog):
    caplog.set_level(logging.DEBUG)
    provider_account(client, hub["owner"])
    worker = new_daemon(client, hub_db, hub["owner"])
    run_id = gateway_run(client, hub_db, hub["owner"], worker)
    token = gateway_lease(leased(client, worker, run_id))["value"]
    assert relay(client, token).status_code == 200
    (key,) = gateway.key_values()
    run = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}", headers=hub["owner"]).text
    answers = [
        run,
        client.get("/v1/accounts", headers=hub["owner"]).text,
        client.get(path(), headers=hub["owner"]).text,
    ]
    dump = table_dump(hub_db)
    for value in (key, token):
        assert all(value not in text for text in answers)
        assert value not in caplog.text
        assert value not in str(audit_rows(hub_db, "credential.lease"))
        assert value not in dump
