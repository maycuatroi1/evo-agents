"""The weekly budget of a model account, plan token-and-quota-economy, criterion a1 (``evo_agents.hub.quota``,
``evo_agents.hub.server.accounts.service.budgets``).

An owner gives an account ``weekly_budget_usd`` and, if they like, ``weekly_reset_at``, the UTC instant its weeks start
from (Monday 00:00 UTC without one). The cost of the runs that took the account since its week began, over the budget,
is its ``seven_day`` window, reset when the week ends: on /v1/accounts, on ``hub account list|show`` and on the
account pools that hold it. A run that took the account before the week began counts in no window; null drops the
budget; a budget of 0 or past MAX_BUDGET_USD is refused.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from evo_agents.hub.quota import budget_window, week_bounds, with_budget
from tests.hub import pg

MONDAY = datetime(2026, 11, 16, tzinfo=UTC)


# The window


def test_weeks_start_on_monday_or_every_seven_days_from_the_reset_given():
    now = datetime(2026, 11, 19, 15, 30, tzinfo=UTC)  # a Thursday
    assert week_bounds(None, now) == (MONDAY, MONDAY + timedelta(days=7))
    anchor = datetime(2026, 1, 3, 6, 0, tzinfo=UTC)  # a Saturday, 06:00
    start, end = week_bounds(anchor, now)
    assert start == datetime(2026, 11, 14, 6, 0, tzinfo=UTC) and end - start == timedelta(days=7)
    later = anchor + timedelta(weeks=60)  # an anchor to come counts its weeks back
    assert week_bounds(later, now)[0] == datetime(2026, 11, 14, 6, 0, tzinfo=UTC)


def test_the_cost_over_the_budget_is_the_seven_day_window():
    end = MONDAY + timedelta(days=7)
    assert budget_window(2.5, 10, end) == {"utilization": 0.25, "resets_at": end.isoformat()}
    assert budget_window(12, 10, end)["utilization"] == 1.0
    reported = {"status": "allowed", "windows": {"five_hour": {"utilization": 0.4}, "seven_day": {"utilization": 0.9}}}
    merged = with_budget(reported, budget_window(2.5, 10, end))
    assert (
        merged["windows"]["seven_day"]["utilization"] == 0.25 and merged["windows"]["five_hour"]["utilization"] == 0.4
    )
    assert (merged["status"], merged["utilization"]) == ("allowed", 0.4)
    spent = with_budget(None, budget_window(10, 10, end))
    assert (spent["status"], spent["rate_limit_type"], spent["resets_at"]) == ("rejected", "seven_day", end.isoformat())
    limited = {"status": "rejected", "rate_limit_type": "five_hour", "resets_at": end.isoformat()}
    assert with_budget(limited, budget_window(1, 10, end))["status"] == "rejected"  # the CLI's limit stays


# Through the hub

needs_db = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
if pg.DSN:
    from tests.hub import test_credentials_api as credentials_api
    from tests.hub.test_accounts_api import account_of, accounts, change, run_on, three
    from tests.hub.test_accounts_figures import by_name, cost
    from tests.hub.test_credentials_api import POOL, PROJECT, audit_rows, worker_of
    from tests.hub.test_run_cli import write_credentials
    from tests.hub.test_runs import report
    from tests.hub.test_secrets_cli import run_cli

    app_key, config, hub, client = (
        credentials_api.app_key,
        credentials_api.config,
        credentials_api.hub,
        credentials_api.client,
    )


def at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@needs_db
def test_the_cost_since_the_week_began_over_the_budget_is_the_accounts_seven_day_window(client, hub, hub_db):
    three(client, hub["owner"])
    worker = worker_of(client, hub["owner"], "mac-mini")
    taken = []
    for name in ("claude-a", "claude-b", "claude-c", "claude-a"):
        run_id = run_on(client, hub["owner"], worker, "evo-agents")
        assert account_of(client, worker, run_id) == name
        assert report(client, worker, run_id, "failed", error="done").status_code == 200
        taken.append(run_id)
    cost(hub_db, taken[0], 3.0)
    cost(hub_db, taken[3], 1.0)
    cost(hub_db, taken[1], 8.0)
    anchor = datetime.now(UTC) - timedelta(days=1)
    budget = change(client, hub["owner"], "claude-a", weekly_budget_usd=10, weekly_reset_at=anchor.isoformat())
    assert budget.status_code == 200, budget.text
    assert budget.json()["weekly_budget_usd"] == 10 and at(budget.json()["weekly_reset_at"]) == anchor
    a = by_name(accounts(client, hub["owner"]))["claude-a"]
    window = a["last_usage"]["windows"]["seven_day"]
    assert window["utilization"] == pytest.approx(0.4) and at(window["resets_at"]) == anchor + timedelta(days=7)
    assert a["last_usage"]["status"] == "allowed" and a["last_usage_at"] is None
    shown = client.get("/v1/accounts/claude-a", headers=hub["owner"]).json()
    assert shown["last_usage"]["windows"]["seven_day"]["utilization"] == pytest.approx(0.4)
    # a run that took the account before its week began counts in no window
    cost(hub_db, taken[0], 3.0, days_ago=2)
    window = by_name(accounts(client, hub["owner"]))["claude-a"]["last_usage"]["windows"]["seven_day"]
    assert window["utilization"] == pytest.approx(0.1)
    # spent: rejected until the week ends; b has no budget and shows none
    assert change(client, hub["owner"], "claude-b", weekly_budget_usd=5).status_code == 200
    b = by_name(accounts(client, hub["owner"]))["claude-b"]
    assert (b["last_usage"]["status"], b["last_usage"]["rate_limit_type"]) == ("rejected", "seven_day")
    monday = at(b["last_usage"]["windows"]["seven_day"]["resets_at"])
    assert (monday.weekday(), monday.hour, monday.minute) == (0, 0, 0) and b["weekly_reset_at"] is None
    assert by_name(accounts(client, hub["owner"]))["claude-c"]["last_usage"] is None
    # null drops it
    dropped = change(client, hub["owner"], "claude-b", weekly_budget_usd=None)
    assert dropped.status_code == 200 and dropped.json()["weekly_budget_usd"] is None
    assert by_name(accounts(client, hub["owner"]))["claude-b"]["last_usage"] is None
    targets = [row[0] for row in audit_rows(hub_db, "account.set")]
    assert any(t.endswith("weekly_budget_usd=10.0") for t in targets)
    assert any(t.endswith("weekly_budget_usd=None") for t in targets)


@needs_db
def test_a_budget_out_of_bounds_or_a_reset_without_a_zone_is_refused(client, hub):
    three(client, hub["owner"])
    for wrong in (
        {"weekly_budget_usd": 0},
        {"weekly_budget_usd": -1},
        {"weekly_budget_usd": 1_000_001},
        {"weekly_reset_at": "2026-11-16T00:00:00"},
    ):
        refused = change(client, hub["owner"], "claude-a", **wrong)
        assert refused.status_code == 422, (wrong, refused.text)


@needs_db
def test_an_account_pool_shows_the_budget_window_of_its_accounts(client, hub, hub_db):
    three(client, hub["owner"])
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = run_on(client, hub["owner"], worker, "evo-agents")
    assert account_of(client, worker, run_id) == "claude-a"
    assert report(client, worker, run_id, "failed", error="done").status_code == 200
    cost(hub_db, run_id, 5.0)
    assert change(client, hub["owner"], "claude-a", weekly_budget_usd=20).status_code == 200
    pool = client.get(f"/v1/projects/{PROJECT}/account-pools/{POOL}", headers=hub["admin"])
    assert pool.status_code == 200, pool.text
    found = {a["name"]: a for a in pool.json()["accounts"]}
    assert found["claude-a"]["last_usage"]["windows"]["seven_day"]["utilization"] == pytest.approx(0.25)
    assert found["claude-b"]["last_usage"] is None


@needs_db
def test_the_cli_sets_the_budget_and_shows_its_window(client, hub, hub_db, tmp_path, monkeypatch, capsys):
    from evo_agents.hub import client as hub_client

    three(client, hub["owner"])
    home = write_credentials(tmp_path / "owner", "https://hub.test", "owner", hub["owner"]["Authorization"][7:])
    sent: list = []

    def call(self, method, target, body=None, *, timeout=None):
        sent.append((method, target, body))
        response = client.request(method, target, json=body, headers=hub["owner"])
        assert response.status_code < 400, response.text
        return response.json() if response.content else None

    monkeypatch.setattr(hub_client.Hub, "call", call)

    def cli(*args):
        return run_cli(monkeypatch, capsys, home, "hub", "account", *args)

    made = cli("set", "claude-a", "--weekly-budget-usd", "12.5", "--weekly-reset-at", "2026-11-14T06:00:00Z")
    assert made.code == 0, made.err
    assert sent[-1][2] == {"weekly_budget_usd": 12.5, "weekly_reset_at": "2026-11-14T06:00:00+00:00"}
    shown = cli("show", "claude-a")
    assert shown.code == 0, shown.err
    assert "Weekly budget:" in shown.out and "seven_day 0%" in shown.out
    listed = cli("list", "--json")
    assert by_name(json.loads(listed.out))["claude-a"]["weekly_budget_usd"] == 12.5
    assert cli("set", "claude-a", "--no-weekly-budget").code == 0 and sent[-1][2] == {"weekly_budget_usd": None}
    both = cli("set", "claude-a", "--no-weekly-budget", "--weekly-budget-usd", "3")
    assert both.code == 2 and "not both" in both.err
