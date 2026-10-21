"""What a member reads of their accounts at work (``evo_agents.hub.server.account_views``): the figures of GET
/v1/accounts and GET /v1/accounts/{name}, the accounts of a run on GET .../runs/{id}/accounts, and
``evo-agents hub account show``.

The checks the claude-account-rotation plan names for its fourth step, on the hub's side: each account shows the last
usage the CLI reported, its runs and their estimated cost over 24 hours and 7 days, and the runs holding it; a run's
page reads the account it took by name, how, and each usage limit it hit with the account it moved to. Around them: a
member reads their own alone, a hub admin too, and nothing carries a value."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import update

from evo_agents.hub import tables
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import sql
from tests.hub.test_accounts_api import VAR, account_of, accounts, run_on, three
from tests.hub.test_credentials_api import PROJECT, env_secret, sample, worker_of
from tests.hub.test_rate_limit_failover import RESET, answer_of, lease_of
from tests.hub.test_runs import report

app_key, config, hub, client = (
    credentials_api.app_key,
    credentials_api.config,
    credentials_api.hub,
    credentials_api.client,
)


def by_name(found: list[dict]) -> dict[str, dict]:
    return {account["name"]: account for account in found}


def cost(db, run_id: int, usd: float, days_ago: float = 0) -> None:
    """Run ``run_id`` reports ``usd`` in its usage, and its leases were issued ``days_ago`` days ago."""
    sql(db, update(tables.runs).where(tables.runs.c.id == run_id).values(usage={"total_cost_usd": usd}))
    if days_ago:
        cl = tables.credential_leases
        at = datetime.now(UTC) - timedelta(days=days_ago)
        sql(db, update(cl).where(cl.c.run_id == run_id).values(issued_at=at))


def run_accounts(client, headers, run_id: int):
    return client.get(f"/v1/projects/{PROJECT}/runs/{run_id}/accounts", headers=headers)


def test_each_account_shows_its_runs_cost_usage_and_the_runs_holding_it(client, hub, hub_db):
    values = three(client, hub["owner"])
    worker = worker_of(client, hub["owner"], "mac-mini", slots=3)
    # three runs in a row take a, b and c; a's run ended 3 days ago, b's an hour ago, c's holds it now
    old = run_on(client, hub["owner"], worker, "evo-agents")
    assert account_of(client, worker, old) == "claude-a"
    assert report(client, worker, old, "failed", error="done").status_code == 200
    cost(hub_db, old, 1.5, days_ago=3)
    recent = run_on(client, hub["owner"], worker, "evo-agents")
    assert account_of(client, worker, recent) == "claude-b"
    assert report(client, worker, recent, "failed", error="done").status_code == 200
    cost(hub_db, recent, 0.25)
    held = run_on(client, hub["owner"], worker, "evo-agents")
    assert account_of(client, worker, held) == "claude-c"
    lease_id = lease_of(client, worker, held)
    windows = {"five_hour": {"utilization": 0.85, "resets_at": RESET.isoformat()}}
    answer_of(client, worker, held, lease_id, "allowed_warning", utilization=0.85, windows=windows)

    found = by_name(accounts(client, hub["owner"]))
    a, b, c = found["claude-a"], found["claude-b"], found["claude-c"]
    assert (a["runs_24h"], a["runs_7d"], a["cost_24h_usd"], a["cost_7d_usd"]) == (0, 1, 0, 1.5)
    assert (b["runs_24h"], b["runs_7d"], b["cost_24h_usd"], b["cost_7d_usd"]) == (1, 1, 0.25, 0.25)
    assert (c["runs_24h"], c["runs_7d"], c["cost_7d_usd"]) == (1, 1, 0)
    assert a["held_by"] == [] and b["held_by"] == []
    assert c["held_by"] == [{"run_id": held, "project": PROJECT, "state": "leased"}]
    assert a["last_usage"] is None and a["last_usage_at"] is None
    assert c["last_usage"]["status"] == "allowed_warning" and c["last_usage"]["utilization"] == 0.85
    assert c["last_usage"]["windows"]["five_hour"]["utilization"] == 0.85 and c["last_usage_at"] is not None

    # a run older than 7 days counts no more
    cost(hub_db, old, 1.5, days_ago=8)
    one = client.get("/v1/accounts/claude-a", headers=hub["owner"])
    assert one.status_code == 200 and one.headers["cache-control"] == "no-store"
    assert (one.json()["runs_7d"], one.json()["cost_7d_usd"]) == (0, 0)
    listed = by_name(accounts(client, hub["owner"]))["claude-c"]
    assert client.get("/v1/accounts/claude-c", headers=hub["owner"]).json() == listed
    for value in values.values():
        assert value not in json.dumps(found)


def test_a_member_reads_their_own_account_alone(client, hub):
    env_secret(client, hub["other"], "claude-theirs", VAR, sample("theirs-"))
    for who in ("owner", "admin"):
        response = client.get("/v1/accounts/claude-theirs", headers=hub[who])
        assert response.status_code == 404 and "no env secret named claude-theirs" in response.text
    assert client.get("/v1/accounts/claude-theirs", headers=hub["other"]).status_code == 200


def test_a_run_shows_its_account_and_each_failover_by_name_to_its_owner_alone(client, hub, hub_db):
    values = three(client, hub["owner"])
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = run_on(client, hub["owner"], worker, "evo-agents")
    assert account_of(client, worker, run_id) == "claude-a"
    response = run_accounts(client, hub["owner"], run_id)
    assert response.status_code == 200, response.text
    (taken,) = response.json()
    assert (taken["env_var"], taken["account"], taken["how"], taken["failovers"], taken["events"]) == (
        VAR,
        "claude-a",
        "pool",
        0,
        [],
    )

    answer_of(client, worker, run_id, lease_of(client, worker, run_id), resets_at=RESET.isoformat())
    (taken,) = run_accounts(client, hub["owner"], run_id).json()
    assert (taken["account"], taken["failovers"]) == ("claude-b", 1)
    (event,) = taken["events"]
    assert (event["action"], event["account"], event["to"]) == ("failover", "claude-a", "claude-b")
    assert datetime.fromisoformat(event["cooldown_until"]) == RESET

    # a cools down and c is paused: the next limit stops the run, and says so
    assert client.patch("/v1/accounts/claude-c", json={"paused": True}, headers=hub["owner"]).status_code == 200
    answer = answer_of(client, worker, run_id, lease_of(client, worker, run_id), resets_at=RESET.isoformat())
    assert answer["action"] == "stop"
    (taken,) = run_accounts(client, hub["owner"], run_id).json()
    assert [(e["action"], e["account"], e["to"]) for e in taken["events"]] == [
        ("failover", "claude-a", "claude-b"),
        ("stop", "claude-b", None),
    ]
    for value in values.values():
        assert value not in json.dumps(taken)

    # the other member, a writer of the project, and a hub admin see the run, not its accounts
    for who in ("other", "admin"):
        refused = run_accounts(client, hub[who], run_id)
        assert refused.status_code in (403, 404)
    assert "who dispatched run" in run_accounts(client, hub["other"], run_id).text
    assert run_accounts(client, hub["owner"], 999_999).status_code == 404
