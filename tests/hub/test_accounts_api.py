"""A member's accounts: several env secrets for one variable, the routes under /v1/accounts and /v1/account-policies
(``evo_agents.hub.server.accounts``), and which of them each run gets (``evo_agents.hub.server.account_choice``).

The checks the claude-account-rotation plan names for its first step: round-robin spreads consecutive runs evenly,
weighted round-robin follows the weights and leaves out weight 0, fill-first takes the lowest priority still usable,
a paused or cooling account is skipped, every account cooling gives the one whose cooldown ends first and a run event
that says so, a run keeps its account across every ask, an account bound to the worker comes before the pool, a run of
the Curator rotates among its charter's accounts alone, and one secret per variable without a policy leases as before.
Around them: a member sees and changes their own accounts and policies alone, a hub admin too, and no answer, audit
row or run event holds a value or names an account but by its id."""

import json
from collections import Counter
from datetime import timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, insert, select, update

from evo_agents.hub import tables
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import sql
from tests.hub.test_credentials_api import (
    PLAN,
    PROJECT,
    STEPS,
    audit_rows,
    by_name,
    env_secret,
    give_back,
    leased,
    sample,
    secret_ids,
    worker_of,
)
from tests.hub.test_runs import report

# the hub and its client of test_credentials_api, as fixtures of this module
app_key, config, hub, client = (
    credentials_api.app_key,
    credentials_api.config,
    credentials_api.hub,
    credentials_api.client,
)

VAR = "CLAUDE_CODE_OAUTH_TOKEN"
READER = {"role": "reader", "max_level": "internal"}


def accounts(client, headers) -> list[dict]:
    response = client.get("/v1/accounts", headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def change(client, headers, name: str, **body):
    return client.patch(f"/v1/accounts/{name}", json=body, headers=headers)


def set_policy(client, headers, **body) -> dict:
    response = client.put(f"/v1/account-policies/{PROJECT}", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def three(client, headers, **extra) -> dict[str, str]:
    """The owner's accounts claude-a, claude-b and claude-c for VAR, with their values."""
    values = {name: sample(f"{name}-") for name in ("claude-a", "claude-b", "claude-c")}
    for name, value in values.items():
        env_secret(client, headers, name, VAR, value, **extra.get(name, {}))
    return values


def run_on(client, headers, worker: dict, repo: str) -> int:
    """A run of the step on ``repo``, claimed by ``worker`` (a step for each repo, so runs can be out at once)."""
    body = {"plan_id": PLAN, "steps": [STEPS[repo]], "worker_id": worker["id"]}
    response = client.post(f"/v1/projects/{PROJECT}/runs", json=body, headers=headers)
    assert response.status_code == 201, response.text
    run_id = response.json()[0]["id"]
    response = client.post("/v1/worker/claim", json={"wait_s": 0}, headers=worker["headers"])
    assert response.json()["run"]["id"] == run_id
    return run_id


def account_of(client, worker: dict, run_id: int) -> str | None:
    """The account the run got for VAR, by name; None for none."""
    names = [lease["name"] for lease in leased(client, worker, run_id)["leases"] if lease.get("env_var") == VAR]
    assert len(names) <= 1
    return names[0] if names else None


def consecutive(client, headers, worker: dict, count: int) -> list[str | None]:
    """The accounts ``count`` runs get one after the other, each run ending before the next one is dispatched."""
    taken = []
    for _ in range(count):
        run_id = run_on(client, headers, worker, "evo-agents")
        taken.append(account_of(client, worker, run_id))
        assert report(client, worker, run_id, "failed", error="done").status_code == 200
    return taken


def cool(db, name: str, minutes: int) -> None:
    """Account ``name`` cools down for ``minutes`` from now, as a usage limit would make it."""
    (secret_id,) = sql(db, select(tables.secrets.c.id).where(tables.secrets.c.name == name))[0]
    a = tables.accounts
    until = func.now() + timedelta(minutes=minutes)
    found = sql(db, select(a.c.secret_id).where(a.c.secret_id == secret_id))
    if found:
        sql(db, update(a).values(cooldown_until=until, cooldown_reason="usage limit").where(a.c.secret_id == secret_id))
    else:
        sql(db, insert(a).values(secret_id=secret_id, cooldown_until=until, cooldown_reason="usage limit"))


def events(db, run_id: int) -> list[dict]:
    e = tables.run_events
    return [row[0] for row in sql(db, select(e.c.body).where(e.c.run_id == run_id, e.c.kind == "system"))]


def taken_how(db, run_id: int) -> list[tuple]:
    ra, s = tables.run_accounts, tables.secrets
    return sql(
        db,
        select(ra.c.env_var, s.c.name, ra.c.how)
        .join_from(ra, s, s.c.id == ra.c.secret_id)
        .where(ra.c.run_id == run_id)
        .order_by(ra.c.env_var),
    )


# Which account a run gets


def test_one_secret_per_variable_without_a_policy_leases_as_before(client, hub, hub_db):
    oauth, api = sample("oauth-"), sample("api-")
    env_secret(client, hub["owner"], "claude-oauth", VAR, oauth)
    env_secret(client, hub["owner"], "api", "API_TOKEN", api)
    worker = worker_of(client, hub["owner"], "mac-mini")
    for _ in range(3):
        run_id = run_on(client, hub["owner"], worker, "evo-agents")
        leases = by_name(leased(client, worker, run_id))
        assert {name: (lease["env_var"], lease["value"]) for name, lease in leases.items()} == {
            "claude-oauth": (VAR, oauth),
            "api": ("API_TOKEN", api),
        }
        assert taken_how(hub_db, run_id) == [("API_TOKEN", "api", "pool"), (VAR, "claude-oauth", "pool")]
        assert events(hub_db, run_id) == []
        ids = secret_ids(hub_db, "api", "claude-oauth")
        assert audit_rows(hub_db, "credential.lease")[-1][0].startswith(
            f"{PROJECT}/{PLAN}#1 run:{run_id} secrets={ids} "
        )
        assert report(client, worker, run_id, "failed", error="done").status_code == 200


def test_round_robin_spreads_consecutive_runs_and_a_run_keeps_its_account(client, hub, hub_db):
    three(client, hub["owner"])
    worker = worker_of(client, hub["owner"], "mac-mini", slots=4)
    out = {repo: run_on(client, hub["owner"], worker, repo) for repo in ("evo-agents", "m1-kb-docs", "m1-identity")}
    first = {repo: account_of(client, worker, run_id) for repo, run_id in out.items()}
    assert sorted(first.values()) == ["claude-a", "claude-b", "claude-c"]
    # asked again, given back and asked again, paused meanwhile: the same account
    run_id = out["evo-agents"]
    assert change(client, hub["owner"], first["evo-agents"], paused=True).status_code == 200
    assert account_of(client, worker, run_id) == first["evo-agents"]
    assert give_back(client, worker["headers"], run_id).status_code == 200
    assert account_of(client, worker, run_id) == first["evo-agents"]
    assert change(client, hub["owner"], first["evo-agents"], paused=False).status_code == 200
    # the fourth run starts the next round
    fourth = run_on(client, hub["owner"], worker, "notes")
    assert account_of(client, worker, fourth) == "claude-a"


def test_weighted_round_robin_follows_the_weights_and_weight_zero_leaves_the_pool(client, hub):
    three(client, hub["owner"])
    set_policy(client, hub["owner"], strategy="weighted-round-robin")
    for name, weight in (("claude-a", 3), ("claude-b", 1), ("claude-c", 0)):
        assert change(client, hub["owner"], name, weight=weight).status_code == 200
    worker = worker_of(client, hub["owner"], "mac-mini")
    assert Counter(consecutive(client, hub["owner"], worker, 8)) == {"claude-a": 6, "claude-b": 2}


def test_fill_first_takes_the_lowest_priority_still_usable(client, hub, hub_db):
    three(client, hub["owner"])
    set_policy(client, hub["owner"], strategy="fill-first")
    for name, priority in (("claude-a", 5), ("claude-b", 1), ("claude-c", 2)):
        assert change(client, hub["owner"], name, priority=priority).status_code == 200
    worker = worker_of(client, hub["owner"], "mac-mini")
    assert consecutive(client, hub["owner"], worker, 2) == ["claude-b", "claude-b"]
    assert change(client, hub["owner"], "claude-b", paused=True).status_code == 200
    assert consecutive(client, hub["owner"], worker, 1) == ["claude-c"]
    cool(hub_db, "claude-c", 30)
    assert consecutive(client, hub["owner"], worker, 1) == ["claude-a"]


def test_paused_and_cooling_accounts_are_skipped_and_all_cooling_takes_the_first_ready(client, hub, hub_db):
    three(client, hub["owner"])
    worker = worker_of(client, hub["owner"], "mac-mini")
    assert change(client, hub["owner"], "claude-a", paused=True).status_code == 200
    cool(hub_db, "claude-b", 30)
    assert consecutive(client, hub["owner"], worker, 2) == ["claude-c", "claude-c"]

    cool(hub_db, "claude-c", 90)
    cool(hub_db, "claude-b", 60)
    run_id = run_on(client, hub["owner"], worker, "evo-agents")
    assert account_of(client, worker, run_id) == "claude-b"  # its cooldown ends first; claude-a is paused
    assert taken_how(hub_db, run_id) == [(VAR, "claude-b", "cooling")]
    (event,) = events(hub_db, run_id)
    (b_id,) = secret_ids(hub_db, "claude-b").split(",")
    assert event["env_var"] == VAR and event["secret_id"] == int(b_id)
    assert "cooling down" in event["text"] and f"secret {b_id}" in event["text"] and "claude-b" not in event["text"]
    assert account_of(client, worker, run_id) == "claude-b" and len(events(hub_db, run_id)) == 1  # kept, said once
    assert report(client, worker, run_id, "failed", error="done").status_code == 200

    for name in ("claude-b", "claude-c"):
        assert change(client, hub["owner"], name, paused=True).status_code == 200
    run_id = run_on(client, hub["owner"], worker, "evo-agents")
    assert account_of(client, worker, run_id) is None
    (event,) = events(hub_db, run_id)
    assert event["secret_id"] is None and "paused" in event["text"]


def test_an_account_bound_to_the_worker_comes_before_the_pool(client, hub):
    worker = worker_of(client, hub["owner"], "mac-mini")
    three(client, hub["owner"], **{"claude-c": {"workers": ["mac-mini"]}})
    worker_of(client, hub["owner"], "desk")
    assert consecutive(client, hub["owner"], worker, 2) == ["claude-c", "claude-c"]
    assert change(client, hub["owner"], "claude-c", paused=True).status_code == 200
    assert consecutive(client, hub["owner"], worker, 3) == ["claude-a", "claude-b", "claude-a"]


def test_two_runs_asking_at_once_take_two_turns(client, hub):
    three(client, hub["owner"])
    worker = worker_of(client, hub["owner"], "mac-mini", slots=3)
    out = [run_on(client, hub["owner"], worker, repo) for repo in ("evo-agents", "m1-kb-docs", "m1-identity")]

    async def ask_all():
        import asyncio

        import httpx

        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://hub.test") as each:
            asked = [each.post(f"/v1/worker/runs/{run_id}/credentials", headers=worker["headers"]) for run_id in out]
            return await asyncio.gather(*asked)

    answers = client.portal.call(ask_all)
    names = [lease["name"] for answer in answers for lease in answer.json()["leases"]]
    assert sorted(names) == ["claude-a", "claude-b", "claude-c"]


# The routes


def test_a_member_lists_and_changes_their_own_accounts_alone(client, hub, hub_db):
    values = three(client, hub["owner"])
    theirs = sample("theirs-")
    env_secret(client, hub["other"], "claude-a", VAR, theirs)  # the same name, another member's
    listed = accounts(client, hub["owner"])
    assert [(a["name"], a["env_var"], a["state"], a["weight"], a["priority"]) for a in listed] == [
        ("claude-a", VAR, "ready", 1, 0),
        ("claude-b", VAR, "ready", 1, 0),
        ("claude-c", VAR, "ready", 1, 0),
    ]
    assert listed[0]["projects"] == [PROJECT] and listed[0]["workers"] == []
    changed = change(client, hub["owner"], "claude-b", weight=4, priority=2)
    assert changed.status_code == 200, changed.text
    assert (changed.json()["weight"], changed.json()["priority"], changed.json()["state"]) == (4, 2, "ready")
    paused = change(client, hub["owner"], "claude-b", paused=True).json()
    assert paused["state"] == "paused" and paused["paused_at"] is not None and paused["weight"] == 4
    assert change(client, hub["owner"], "claude-b", paused=False).json()["state"] == "ready"
    # another member's account of the same name is untouched; a hub admin sees their own alone
    assert [a["weight"] for a in accounts(client, hub["other"])] == [1]
    assert accounts(client, hub["admin"]) == []
    for who in ("other", "admin"):
        assert change(client, hub[who], "claude-b", weight=9).status_code == 404
    assert change(client, hub["owner"], "nothing", weight=1).status_code == 404
    assert change(client, hub["owner"], "claude-a", weight=1001).status_code == 422
    assert change(client, hub["owner"], "claude-a", value="x").status_code == 422

    b_id = secret_ids(hub_db, "claude-b")
    assert [row[:3] for row in audit_rows(hub_db, "account.set")] == [
        (f"secret:{b_id} weight=4 priority=2 paused=no", "owner", None),
        (f"secret:{b_id} weight=4 priority=2 paused=yes", "owner", None),
        (f"secret:{b_id} weight=4 priority=2 paused=no", "owner", None),
    ]
    dumped = json.dumps([listed, accounts(client, hub["other"]), sql(hub_db, select(tables.audit.c.target))])
    for value in [*values.values(), theirs]:
        assert value not in dumped


def test_a_policy_is_the_members_own_for_a_project(client, hub, hub_db):
    path = f"/v1/account-policies/{PROJECT}"
    default = client.get(path, headers=hub["owner"]).json()
    assert default == {
        "project": PROJECT,
        "strategy": "round-robin",
        "cooldown_seconds": 3600,
        "failover": True,
        "max_failovers": 3,
        "updated_at": None,
    }
    assert client.get("/v1/account-policies", headers=hub["owner"]).json() == []
    written = set_policy(client, hub["owner"], strategy="fill-first", cooldown_seconds=600, failover=False)
    assert (written["strategy"], written["cooldown_seconds"], written["failover"], written["max_failovers"]) == (
        "fill-first",
        600,
        False,
        3,
    )
    assert written["updated_at"] is not None
    assert client.get("/v1/account-policies", headers=hub["owner"]).json() == [written]
    # someone-else and a hub admin keep the defaults: a policy is its member's alone
    for who in ("other", "admin"):
        assert client.get(path, headers=hub[who]).json() == default
    for wrong in ({"strategy": "random"}, {"cooldown_seconds": 30}, {"max_failovers": 21}, {"extra": 1}):
        assert client.put(path, json=wrong, headers=hub["owner"]).status_code == 422
    assert client.get("/v1/account-policies/nowhere", headers=hub["owner"]).status_code == 404
    grant = client.put(f"/v1/admin/projects/{PROJECT}/grants/someone-else", json=READER, headers=hub["admin"])
    assert grant.status_code == 200, grant.text
    assert client.put(path, json={}, headers=hub["other"]).status_code == 403

    assert client.delete(path, headers=hub["owner"]).status_code == 204
    assert client.get(path, headers=hub["owner"]).json() == default
    assert client.delete(path, headers=hub["owner"]).status_code == 404
    assert [row[:3] for row in audit_rows(hub_db, "account.policy-put")] == [
        (f"{PROJECT} strategy=fill-first cooldown=600 failover=no max_failovers=3", "owner", PROJECT)
    ]
    assert [row[:3] for row in audit_rows(hub_db, "account.policy-delete")] == [(PROJECT, "owner", PROJECT)]
