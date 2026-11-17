"""The quota of a Z.ai provider account, plan token-and-quota-economy, criterion a1 (``evo_agents.hub.quota``,
``evo_agents.hub.server.provider_quota``).

The hub reads GET <origin>/api/monitor/usage/quota/limit with the account's key as a Bearer token, pinned to the public
address it checked, for a member's account and an agent's alike; it maps the five-hour limit (unit 3, or 5_HOUR) to
``windows.five_hour`` and the weekly one (unit 6, or WEEKLY) to ``windows.seven_day``, each a share from 0 to 1 that
resets at a UTC time, keeps them in ``accounts.last_usage`` and ``last_usage_at``, and cools the account down until
the reset of a window used up. An error never carries the key. POST /v1/provider-accounts/{name}/quota, the route of
an agent's account, ``evo-agents hub provider-account quota`` and ``GatewaySync`` (every QUOTA_INTERVAL seconds and
after a sweep took back keys) read it. Z.ai is httpx.MockTransport and the resolver a dict, both on the app's state.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from evo_agents.hub.quota import zai_usage, zai_windows
from tests.hub import pg

FIVE_RESET = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
WEEK_RESET = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)


def ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def limits(five: float, week: float, *, five_reset: datetime = FIVE_RESET, week_reset: datetime = WEEK_RESET) -> dict:
    """An answer of Z.ai's quota API: the five-hour and weekly token limits and the monthly calls of its tools."""
    return {
        "code": 200,
        "success": True,
        "data": {
            "limits": [
                {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": five, "nextResetTime": ms(five_reset)},
                {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": week, "nextResetTime": ms(week_reset)},
                {"type": "TIME_LIMIT", "unit": 5, "number": 1, "percentage": 40, "currentValue": 400, "usage": 1000},
            ]
        },
    }


# What an answer says


def test_units_and_names_map_to_the_five_hour_and_weekly_windows():
    windows = zai_windows(limits(25, 60))
    assert windows == {
        "five_hour": {"utilization": 0.25, "resets_at": FIVE_RESET.isoformat()},
        "seven_day": {"utilization": 0.6, "resets_at": WEEK_RESET.isoformat()},
    }
    named = {"data": {"limits": [{"unit": "5_HOUR", "percentage": 10}, {"type": "WEEKLY", "percentage": 100}]}}
    assert zai_windows(named) == {"five_hour": {"utilization": 0.1}, "seven_day": {"utilization": 1.0}}
    usage = zai_usage(limits(25, 60))
    assert (usage["status"], usage["utilization"]) == ("allowed", 0.6)


def test_a_window_used_up_rejects_until_it_resets():
    usage = zai_usage(limits(100, 70))
    assert (usage["status"], usage["rate_limit_type"], usage["resets_at"]) == (
        "rejected",
        "five_hour",
        FIVE_RESET.isoformat(),
    )
    both = zai_usage(limits(100, 100))
    assert (both["rate_limit_type"], both["resets_at"]) == ("seven_day", WEEK_RESET.isoformat())


def test_an_answer_without_a_window_is_none():
    for body in ({}, {"data": None}, {"data": {"limits": [{"unit": 5, "percentage": 3}]}}, [], "no"):
        assert zai_usage(body) is None


# Through the hub

needs_db = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
if pg.DSN:
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    from evo_agents.hub import tables
    from evo_agents.hub.server.app import create_app
    from evo_agents.hub.server.gateway_sync import GatewaySync
    from evo_agents.hub.server.provider_quota import ZAI_REASON
    from evo_agents.hub.server.sealing import Sealer
    from tests.hub import live
    from tests.hub.live import sql
    from tests.hub.test_provider_accounts import KEY, PUBLIC, hub, resolve, sample, stored, written  # noqa: F401
    from tests.hub.test_run_cli import write_credentials
    from tests.hub.test_runs import OWNER, PROJECT, add_worker
    from tests.hub.test_secrets_cli import run_cli

QUOTA_PATH = "/api/monitor/usage/quota/limit"
AGENT = f"/v1/projects/{PROJECT}/agents/auditor/provider-accounts" if pg.DSN else ""


class ZaiQuota:
    """Z.ai's quota API as the hub reaches it: the answer set for a key, else 401 echoing the key it got."""

    def __init__(self) -> None:
        self.answers: dict[str, dict] = {}
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        got = request.headers.get("authorization", "").removeprefix("Bearer ")
        if request.url.path != QUOTA_PATH or got not in self.answers:
            return httpx.Response(401, json={"error": {"message": f"token expired or incorrect: {got}"}})
        return httpx.Response(200, json=self.answers[got])


@pytest.fixture
def endpoint() -> ZaiQuota:
    return ZaiQuota()


@pytest.fixture
def client(hub_db, tmp_path, github, endpoint):
    config = live.hub_config(hub_db, tmp_path, github, secrets_key=KEY)
    app = create_app(config)
    app.state.provider_transport = httpx.MockTransport(endpoint)
    app.state.provider_resolver = resolve
    with TestClient(app, base_url="https://hub.test") as client:
        yield client


def zai(key: str) -> dict:
    return {"provider": "zai", "projects": [PROJECT], "value": key, "models": ["glm-5.3"]}


def agent_zai(client, headers, key: str):
    """The agent @auditor's Z.ai account zai-agent, bound to a worker of the owner's."""
    add_worker(client, headers, "box")
    body = {"provider": "zai", "value": key, "workers": ["box"], "models": ["glm-5.3"]}
    made = client.put(f"{AGENT}/zai-agent", json=body, headers=headers)
    assert made.status_code == 200, made.text


def account_row(db, secret_id: int) -> tuple:
    a = tables.accounts
    query = select(a.c.last_usage, a.c.last_usage_at, a.c.cooldown_until, a.c.cooldown_reason)
    found = sql(db, query.where(a.c.secret_id == secret_id))
    return found[0] if found else (None, None, None, None)


def quota(client, headers, name: str):
    response = client.post(f"/v1/provider-accounts/{name}/quota", headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response


@needs_db
def test_a_members_zai_account_is_read_with_its_key_pinned_and_kept_as_its_last_usage(client, hub, hub_db, endpoint):
    key = sample("zai-")
    written(client, hub["owner"], "zai-main", zai(key))
    endpoint.answers[key] = limits(25, 60)
    response = quota(client, hub["owner"], "zai-main")
    found = response.json()
    assert found["ok"] is True and found["status"] == 200 and key not in response.text
    windows = found["usage"]["windows"]
    assert windows["five_hour"]["utilization"] == 0.25 and windows["seven_day"]["utilization"] == 0.6
    assert datetime.fromisoformat(windows["seven_day"]["resets_at"].replace("Z", "+00:00")) == WEEK_RESET
    (request,) = endpoint.seen
    assert (request.method, request.url.host, request.url.path) == ("GET", PUBLIC, QUOTA_PATH)
    assert request.headers["host"] == "api.z.ai" and request.headers["authorization"] == f"Bearer {key}"
    last_usage, last_usage_at, cooldown_until, _ = account_row(hub_db, stored(hub_db, "zai-main")[0])
    assert last_usage["windows"]["five_hour"] == {"utilization": 0.25, "resets_at": FIVE_RESET.isoformat()}
    assert last_usage["status"] == "allowed" and last_usage_at is not None and cooldown_until is None


@needs_db
def test_a_window_used_up_cools_the_account_until_its_reset_and_room_again_ends_that_cooldown(
    client, hub, hub_db, endpoint
):
    key = sample("zai-")
    written(client, hub["owner"], "zai-main", zai(key))
    secret_id = stored(hub_db, "zai-main")[0]
    reset = (datetime.now(UTC) + timedelta(hours=3)).replace(microsecond=0)
    endpoint.answers[key] = limits(100, 70, five_reset=reset)
    assert quota(client, hub["owner"], "zai-main").json()["usage"]["status"] == "rejected"
    last_usage, _, cooldown_until, reason = account_row(hub_db, secret_id)
    assert last_usage["rate_limit_type"] == "five_hour" and cooldown_until == reset
    assert reason.startswith(ZAI_REASON)
    endpoint.answers[key] = limits(20, 70)
    assert quota(client, hub["owner"], "zai-main").json()["usage"]["status"] == "allowed"
    assert account_row(hub_db, secret_id)[2:] == (None, None)


@needs_db
def test_an_error_never_carries_the_key(client, hub, hub_db, endpoint, caplog):
    caplog.set_level(logging.DEBUG)
    key = sample("zai-")
    written(client, hub["owner"], "zai-main", zai(key))
    response = quota(client, hub["owner"], "zai-main")  # Z.ai refuses it and echoes the key
    found = response.json()
    assert (found["ok"], found["status"], found["usage"]) == (False, 401, None)
    assert found["error"].startswith("HTTP 401") and key not in response.text and key not in caplog.text
    assert account_row(hub_db, stored(hub_db, "zai-main")[0])[0] is None
    other = sample("sk-or-v1-")
    written(client, hub["owner"], "or-main", {**zai(other), "provider": "openrouter"})
    not_zai = quota(client, hub["owner"], "or-main").json()
    assert not_zai["ok"] is False and "not one of Z.ai" in not_zai["error"]
    assert client.post("/v1/provider-accounts/nothing/quota", headers=hub["owner"]).status_code == 404
    assert client.post("/v1/provider-accounts/zai-main/quota", headers=hub["other"]).status_code == 404


@needs_db
def test_an_agents_zai_account_is_read_by_any_member_of_the_project(client, hub, hub_db, endpoint):
    key = sample("zai-")
    agent_zai(client, hub["owner"], key)
    endpoint.answers[key] = limits(5, 15)
    response = client.post(f"{AGENT}/zai-agent/quota", headers=hub["other"])
    assert response.status_code == 200, response.text
    assert response.json()["usage"]["windows"]["seven_day"]["utilization"] == 0.15 and key not in response.text
    assert endpoint.seen[-1].headers["authorization"] == f"Bearer {key}"
    assert account_row(hub_db, stored(hub_db, "zai-agent")[0])[0]["windows"]["five_hour"]["utilization"] == 0.05


@needs_db
def test_gateway_sync_reads_the_quotas_every_interval_and_after_a_sweep(client, hub, hub_db, endpoint, monkeypatch):
    from evo_agents.hub.server import gateway_leases, gateway_sync

    key = sample("zai-")
    written(client, hub["owner"], "zai-main", zai(key))
    endpoint.answers[key] = limits(30, 40)
    swept = iter([0, 0, 2])

    async def sweep(gateway, engine):
        return next(swept)

    async def reconcile(*args):
        return {}

    monkeypatch.setattr(gateway_leases, "sweep", sweep)
    monkeypatch.setattr(gateway_sync, "reconcile", reconcile)

    async def rounds() -> list[int]:
        engine = create_async_engine(hub_db.dsn.replace("postgresql://", "postgresql+psycopg://", 1))
        sync = GatewaySync(object(), engine, Sealer(KEY), "s" * 32)
        sync.quota_hooks = (httpx.MockTransport(endpoint), resolve)
        sync.quota_interval = 3600
        seen = []
        try:
            for _ in range(3):  # the first round reads; the second is within the interval; the third swept keys
                await sync.round()
                seen.append(len(endpoint.seen))
        finally:
            await engine.dispose()
        return seen

    assert asyncio.run(rounds()) == [1, 1, 2]
    assert account_row(hub_db, stored(hub_db, "zai-main")[0])[0]["windows"]["seven_day"]["utilization"] == 0.4


@needs_db
def test_the_cli_reads_a_quota_of_the_members_or_an_agents_account_without_printing_the_key(
    client, hub, hub_db, endpoint, tmp_path, monkeypatch, capsys
):
    from evo_agents.hub import client as hub_client

    home = write_credentials(tmp_path / "owner", "https://hub.test", OWNER, hub["owner"]["Authorization"][7:])

    def call(self, method, target, body=None, *, timeout=None):
        response = client.request(method, target, json=body, headers=hub["owner"])
        assert response.status_code < 400, response.text
        return response.json() if response.content else None

    monkeypatch.setattr(hub_client.Hub, "call", call)
    mine, theirs = sample("zai-"), sample("zai-")
    written(client, hub["owner"], "zai-main", zai(mine))
    agent_zai(client, hub["owner"], theirs)
    endpoint.answers |= {mine: limits(25, 60), theirs: limits(50, 10)}

    def cli(*args):
        return run_cli(monkeypatch, capsys, home, "hub", "provider-account", "quota", *args)

    read = cli("zai-main")
    assert read.code == 0, read.err
    assert "zai-main: allowed" in read.out and "five_hour: 25% used" in read.out and "seven_day: 60% used" in read.out
    agent = cli("zai-agent", "--agent", "@auditor", "--project", PROJECT, "--json")
    assert agent.code == 0, agent.err
    assert '"utilization": 0.5' in agent.out
    endpoint.answers.pop(mine)
    failed = cli("zai-main")
    assert failed.code == 1 and "HTTP 401" in failed.err
    for result in (read, agent, failed):
        assert mine not in result.out + result.err and theirs not in result.out + result.err
