"""A run's account hits its usage limit: POST /v1/worker/runs/{id}/rate-limit (``evo_agents.hub.server.rate_limits``)
and ``account_choice.fail_over``.

The checks the claude-account-rotation plan names for its third step, on the hub's side: a report that is not
rejected keeps the usage alone; a rejected one cools the account down until the reset the CLI gave, else for the
policy's cooldown_seconds, revokes the run's old lease and moves the run to the next account by the rules, which its
next ask for leases carries, and an event and an audit row record the switch by secret id; an account picked by hand,
failover off, max_failovers reached or no account ready answers stop with the earliest time one is ready. Around them:
a retry answers the same move, a lease that is not the run's is 404, a daemon without the protocol header 426, and no
answer, event or audit row holds a value or an account's name."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import tables
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import sql
from tests.hub.test_accounts_api import VAR, account_of, cool, events, run_on, set_policy, three
from tests.hub.test_credentials_api import audit_rows, leased, secret_ids, worker_of

app_key, config, hub, client = (
    credentials_api.app_key,
    credentials_api.config,
    credentials_api.hub,
    credentials_api.client,
)

RESET = datetime(2031, 5, 1, 12, 0, tzinfo=UTC)


def lease_of(client, worker: dict, run_id: int) -> int:
    (lease,) = [lease for lease in leased(client, worker, run_id)["leases"] if lease.get("env_var") == VAR]
    return lease["id"]


def limited(client, worker: dict, run_id: int, lease_id: int, status: str = "rejected", **extra):
    body = {"lease_id": lease_id, "status": status, "rate_limit_type": "five_hour", **extra}
    return client.post(f"/v1/worker/runs/{run_id}/rate-limit", json=body, headers=worker["headers"])


def answer_of(client, worker: dict, run_id: int, lease_id: int, status: str = "rejected", **extra) -> dict:
    response = limited(client, worker, run_id, lease_id, status, **extra)
    assert response.status_code == 200, response.text
    return response.json()


def account_row(db, name: str):
    a, s = tables.accounts, tables.secrets
    return sql(
        db,
        select(a.c.cooldown_until, a.c.cooldown_reason, a.c.last_usage, a.c.last_usage_at)
        .join_from(a, s, s.c.id == a.c.secret_id)
        .where(s.c.name == name),
    )[0]


def run_account(db, run_id: int) -> tuple:
    ra, s = tables.run_accounts, tables.secrets
    return sql(
        db,
        select(s.c.name, ra.c.how, ra.c.failovers)
        .join_from(ra, s, s.c.id == ra.c.secret_id)
        .where(ra.c.run_id == run_id, ra.c.env_var == VAR),
    )[0]


def revoked(db, lease_id: int) -> bool:
    cl = tables.credential_leases
    return sql(db, select(cl.c.revoked_at).where(cl.c.id == lease_id))[0][0] is not None


def no_names_or_values(texts: list[str], values: dict[str, str]) -> None:
    for text in texts:
        for name, value in values.items():
            assert name not in text and value not in text, text


def test_a_report_that_is_not_rejected_keeps_the_usage_alone(client, hub, hub_db):
    three(client, hub["owner"])
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = run_on(client, hub["owner"], worker, "evo-agents")
    assert account_of(client, worker, run_id) == "claude-a"
    lease_id = lease_of(client, worker, run_id)
    windows = {"five_hour": {"utilization": 0.85, "resets_at": RESET.isoformat()}}
    answer = answer_of(client, worker, run_id, lease_id, "allowed_warning", utilization=0.85, windows=windows)
    assert answer["action"] == "none" and answer["secret_id"] == int(secret_ids(hub_db, "claude-a"))
    until, reason, usage, usage_at = account_row(hub_db, "claude-a")
    assert until is None and reason is None and usage_at is not None
    assert usage == {
        "status": "allowed_warning",
        "rate_limit_type": "five_hour",
        "utilization": 0.85,
        "windows": {"five_hour": {"utilization": 0.85, "resets_at": "2031-05-01T12:00:00Z"}},
    }
    assert not revoked(hub_db, lease_id) and account_of(client, worker, run_id) == "claude-a"
    assert events(hub_db, run_id) == [] and audit_rows(hub_db, "account.failover") == []


def test_a_rejected_report_cools_the_account_revokes_its_lease_and_fails_over(client, hub, hub_db):
    values = three(client, hub["owner"])
    worker = worker_of(client, hub["owner"], "mac-mini", slots=2)
    run_id = run_on(client, hub["owner"], worker, "evo-agents")
    assert account_of(client, worker, run_id) == "claude-a"
    lease_id = lease_of(client, worker, run_id)
    answer = answer_of(client, worker, run_id, lease_id, resets_at=RESET.isoformat())
    b_id = int(secret_ids(hub_db, "claude-b"))
    assert (answer["action"], answer["secret_id"], answer["failovers"]) == ("failover", b_id, 1)
    assert datetime.fromisoformat(answer["cooldown_until"]) == RESET
    until, reason, usage, _ = account_row(hub_db, "claude-a")
    assert until == RESET and usage["status"] == "rejected"
    assert reason == f"usage limit five_hour, reported by run {run_id}"
    assert revoked(hub_db, lease_id)
    # the next ask carries the new account's value, and the run keeps it
    again = leased(client, worker, run_id)
    (lease,) = [lease for lease in again["leases"] if lease.get("env_var") == VAR]
    assert (lease["name"], lease["value"]) == ("claude-b", values["claude-b"]) and lease["id"] != lease_id
    assert run_account(hub_db, run_id) == ("claude-b", "pool", 1)
    assert account_of(client, worker, run_id) == "claude-b"
    # an event and an audit row record the switch, by secret id
    a_id = int(secret_ids(hub_db, "claude-a"))
    (event,) = events(hub_db, run_id)
    assert event["secret_id"] == a_id and event["env_var"] == VAR
    assert f"secret {a_id}" in event["text"] and f"goes on with secret {b_id} (failover 1)" in event["text"]
    (row,) = audit_rows(hub_db, "account.failover")
    assert row[0].endswith(f" env={VAR} from=secret:{a_id} to=secret:{b_id}") and row[2] == credentials_api.PROJECT
    no_names_or_values([json.dumps(answer), json.dumps(event), row[0]], values)
    # a retry of the same report answers the same move
    retried = answer_of(client, worker, run_id, lease_id, resets_at=RESET.isoformat())
    assert (retried["action"], retried["secret_id"], retried["failovers"]) == ("failover", b_id, 1)
    # a usage report older than the limit, come late, leaves the limit as the cooling account's last usage
    assert answer_of(client, worker, run_id, lease_id, "allowed", utilization=0.5)["action"] == "none"
    until, _, usage, _ = account_row(hub_db, "claude-a")
    assert until == RESET and usage["status"] == "rejected"
    # the next run skips the cooling account
    assert account_of(client, worker, run_on(client, hub["owner"], worker, "notes")) == "claude-c"


def test_a_rejected_report_without_a_reset_cools_for_the_policys_cooldown(client, hub, hub_db):
    three(client, hub["owner"])
    set_policy(client, hub["owner"], cooldown_seconds=600)
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = run_on(client, hub["owner"], worker, "evo-agents")
    before = datetime.now(UTC)
    answer = answer_of(client, worker, run_id, lease_of(client, worker, run_id), rate_limit_type=None)
    assert answer["action"] == "failover"
    until = account_row(hub_db, "claude-a")[0]
    assert before + timedelta(seconds=590) < until < datetime.now(UTC) + timedelta(seconds=610)
    assert account_row(hub_db, "claude-a")[1] == f"usage limit reached, reported by run {run_id}"


def stopped(client, hub_db, worker: dict, run_id: int, name: str, lease_id: int) -> dict:
    answer = answer_of(client, worker, run_id, lease_id, resets_at=RESET.isoformat())
    assert answer["action"] == "stop" and answer["secret_id"] == int(secret_ids(hub_db, name))
    assert not revoked(hub_db, lease_id) and account_of(client, worker, run_id) == name
    assert "goes on with" not in events(hub_db, run_id)[-1]["text"]
    return answer


def test_an_account_picked_by_hand_never_fails_over(client, hub, hub_db):
    three(client, hub["owner"])
    worker = worker_of(client, hub["owner"], "mac-mini")
    body = {"plan_id": credentials_api.PLAN, "steps": [credentials_api.STEPS["evo-agents"]], "account": "claude-b"}
    response = client.post(
        f"/v1/projects/{credentials_api.PROJECT}/runs", json={**body, "worker_id": worker["id"]}, headers=hub["owner"]
    )
    assert response.status_code == 201, response.text
    run_id = response.json()[0]["id"]
    assert client.post("/v1/worker/claim", json={"wait_s": 0}, headers=worker["headers"]).json()["run"]["id"] == run_id
    answer = stopped(client, hub_db, worker, run_id, "claude-b", lease_of(client, worker, run_id))
    assert answer["reason"] == "its account was picked by hand" and answer["failovers"] == 0
    assert datetime.fromisoformat(answer["ready_at"]) == RESET
    assert run_account(hub_db, run_id) == ("claude-b", "manual", 0)
    assert audit_rows(hub_db, "account.failover") == []


def test_failover_off_stops_the_run(client, hub, hub_db):
    three(client, hub["owner"])
    set_policy(client, hub["owner"], failover=False)
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = run_on(client, hub["owner"], worker, "evo-agents")
    answer = stopped(client, hub_db, worker, run_id, "claude-a", lease_of(client, worker, run_id))
    assert answer["reason"] == "the owner's policy turns failover off"
    # the account cools down all the same
    assert account_row(hub_db, "claude-a")[0] == RESET


def test_max_failovers_reached_stops_the_run(client, hub, hub_db):
    three(client, hub["owner"])
    set_policy(client, hub["owner"], max_failovers=1)
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = run_on(client, hub["owner"], worker, "evo-agents")
    assert answer_of(client, worker, run_id, lease_of(client, worker, run_id))["action"] == "failover"
    answer = stopped(client, hub_db, worker, run_id, "claude-b", lease_of(client, worker, run_id))
    assert answer["reason"] == "it failed over 1 times, the most its owner's policy allows"
    assert answer["failovers"] == 1 and run_account(hub_db, run_id) == ("claude-b", "pool", 1)


def test_no_account_ready_stops_the_run_with_the_earliest_reset(client, hub, hub_db):
    three(client, hub["owner"])
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = run_on(client, hub["owner"], worker, "evo-agents")
    lease_id = lease_of(client, worker, run_id)
    cool(hub_db, "claude-b", 30)
    cool(hub_db, "claude-c", 90)
    answer = stopped(client, hub_db, worker, run_id, "claude-a", lease_id)
    assert answer["reason"] == f"no other account for {VAR} is ready"
    soonest = account_row(hub_db, "claude-b")[0]
    assert datetime.fromisoformat(answer["ready_at"]) == soonest < RESET


def test_a_lease_that_is_not_the_runs_is_404_and_a_daemon_without_the_protocol_426(client, hub, hub_db):
    three(client, hub["owner"])
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = run_on(client, hub["owner"], worker, "evo-agents")
    lease_id = lease_of(client, worker, run_id)
    assert limited(client, worker, run_id, lease_id + 1000).status_code == 404
    assert limited(client, worker, run_id, lease_id, status="exhausted").status_code == 422
    other = worker_of(client, hub["owner"], "laptop")
    assert limited(client, other, run_id, lease_id).status_code == 404
    without = {key: value for key, value in worker["headers"].items() if key != "X-Evo-Worker-Protocol"}
    body = {"lease_id": lease_id, "status": "rejected"}
    response = client.post(f"/v1/worker/runs/{run_id}/rate-limit", json=body, headers=without)
    assert response.status_code == 426
    assert account_row(hub_db, "claude-a")[0] is None and not revoked(hub_db, lease_id)
