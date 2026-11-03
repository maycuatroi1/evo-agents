"""The leases of a run under an agent with accounts of its own (``evo_agents.hub.server.credentials``,
``account_choice``), criteria a3, a4 and a7 of the plan agent-accounts, now that an agent's accounts reach its runs
through its account pool (plan agent-dedicated-pools: ``pool_accounts``).

The agent's accounts in its account pool go to every node of its pool: the run is leased one of them and never the
dispatcher's secret for a model variable, whoever dispatches it; every account unusable leaves the run without that
variable, not with the dispatcher's; a variable that is no model's leases the dispatcher's as before; a failover moves
among the pool's accounts alone; an account picked by hand for a variable the agent has accounts of its own for is 422
naming it; the lease is named ``@pool.<pool>:name``, the audit row names the account by id and pool, and no value is in
any audit row, event or message."""

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select, update

from evo_agents.hub import runs, tables
from evo_agents.hub.server.agent_account_mask import mask_values
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import sql, table_dump
from tests.hub.test_accounts_api import cool, events
from tests.hub.test_agent_accounts import added, body, path, profile_id
from tests.hub.test_credentials_api import (
    PLAN,
    PROJECT,
    STEPS,
    audit_rows,
    by_name,
    env_secret,
    leased,
    sample,
)
from tests.hub.test_credentials_api import worker_of as plain_worker
from tests.hub.test_profile_runs import beat
from tests.hub.test_runs import join_pool, report

app_key, config, client = credentials_api.app_key, credentials_api.config, credentials_api.client

VAR = "CLAUDE_CODE_OAUTH_TOKEN"
OTHER_VAR = "API_TOKEN"  # no model's: the dispatcher's, as before
ADMIN_GRANT = {"role": "admin", "max_level": "internal"}
POOL = "auditors"
POOLS = f"/v1/projects/{PROJECT}/account-pools"


@pytest.fixture
def hub(client, github) -> dict:
    """The members of ``test_credentials_api`` with the owner made an admin, and the agent @auditor."""
    headers = credentials_api.members(client, github)
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/owner", json=ADMIN_GRANT, headers=headers["admin"])
    assert granted.status_code == 200, granted.text
    made = client.post(
        f"/v1/projects/{PROJECT}/agents", json={"handle": "auditor", "name": "Auditor"}, headers=headers["owner"]
    )
    assert made.status_code == 201, made.text
    return headers


def worker_of(client, headers, name: str) -> dict:
    """A worker whose daemon gives the claude-code agent of a run under @auditor a config dir of its own
    (``runs.AGENT_HOME``), which such a run needs (``tests.hub.test_claim_agent_home``): a ready node of @auditor's
    pool."""
    worker = plain_worker(client, headers, name)
    beat(client, worker, features=[runs.AGENT_HOME])
    return join_pool(client, worker, "auditor")


def pooled(client, headers, *names: str) -> None:
    """@auditor's accounts ``names`` in its account pool POOL (made at the first call), by an admin of the project."""
    if client.get(f"{POOLS}/{POOL}", headers=headers).status_code == 404:
        made = client.post(POOLS, json={"name": POOL}, headers=headers)
        assert made.status_code == 201, made.text
        attached = client.put(
            f"/v1/projects/{PROJECT}/agents/auditor/account-pool", json={"account_pool": POOL}, headers=headers
        )
        assert attached.status_code == 200, attached.text
    for name in names:
        put = client.put(f"{POOLS}/{POOL}/accounts", json={"name": name, "agent": "auditor"}, headers=headers)
        assert put.status_code == 200, put.text


def lease_name(name: str) -> str:
    """The name of the lease of @auditor's account ``name`` of its account pool."""
    return f"@pool.{POOL}:{name}"


def run_under(client, headers, worker: dict, handle: str = "auditor", repo: str = "evo-agents", **extra) -> int:
    """A run of the step on ``repo`` under agent ``handle``, claimed by ``worker``."""
    request = {"plan_id": PLAN, "steps": [STEPS[repo]], "profile": handle, **extra}
    response = client.post(f"/v1/projects/{PROJECT}/runs", json=request, headers=headers)
    assert response.status_code == 201, response.text
    run_id = response.json()[0]["id"]
    assert client.post("/v1/worker/claim", json={"wait_s": 0}, headers=worker["headers"]).json()["run"]["id"] == run_id
    return run_id


def finish(client, worker: dict, run_id: int) -> None:
    assert report(client, worker, run_id, "failed", error="done").status_code == 200


def for_var(answer: dict, var: str = VAR) -> list[dict]:
    return [lease for lease in answer["leases"] if lease.get("env_var") == var]


def test_a_run_under_the_agent_gets_its_account_never_the_dispatchers_secret_for_that_variable(client, hub, hub_db):
    worker = worker_of(client, hub["owner"], "box")
    mine, theirs, api = sample("agent-"), sample("owner-"), sample("api-")
    added(client, hub["owner"], "shared-name", body(mine, workers=["box"]))
    pooled(client, hub["owner"], "shared-name")
    # the dispatcher's secret for the same variable even has the agent account's name
    env_secret(client, hub["owner"], "shared-name", VAR, theirs)
    env_secret(client, hub["owner"], "api-token", OTHER_VAR, api)
    env_secret(client, hub["other"], "shared-name", VAR, sample("other-"))
    ids = {
        row[1]: row[0]
        for row in sql(
            hub_db, select(tables.secrets.c.id, tables.secrets.c.owner_id).where(tables.secrets.c.name == "api-token")
        )
    }
    (agent_secret,) = sql(hub_db, select(tables.secrets.c.id).where(tables.secrets.c.profile_id.is_not(None)))[0]

    run_id = run_under(client, hub["owner"], worker)
    answer = leased(client, worker, run_id)
    leases = by_name(answer)
    account = lease_name("shared-name")
    assert set(leases) == {account, "api-token"}  # no two leases of a name
    assert leases[account]["value"] == mine and leases[account]["env_var"] == VAR
    assert leases["api-token"]["value"] == api  # a variable of no model: the dispatcher's, through the credential axis
    assert set(leases[account]) == {
        "id", "kind", "provider", "name", "env_var", "url_prefix", "username", "value", "expires_at",
    }  # fmt: skip
    assert leased(client, worker, run_id) == answer  # asked again: the same leases
    (target, _, _, _) = audit_rows(hub_db, "credential.lease")[-1]
    assert f" secrets={next(iter(ids.values()))} " in target
    assert f" agent-accounts={agent_secret}@pool.{POOL}" in target
    assert "shared-name" not in target and mine not in target and theirs not in target
    assert events(hub_db, run_id) == []

    # the owner reads it under the agent's name, apart from the secrets of their own
    shown = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}/credentials", headers=hub["owner"])
    assert shown.status_code == 200, shown.text
    assert sorted(lease["name"] for lease in shown.json()) == ["@auditor:shared-name", "api-token"]
    assert mine not in table_dump(hub_db) and mine not in shown.text
    finish(client, worker, run_id)

    # a member who is no admin dispatches under the agent: the same account, not their own secret for VAR
    run_id = run_under(client, hub["other"], worker)
    leases = by_name(leased(client, worker, run_id))
    assert [(lease["name"], lease["value"]) for lease in for_var(leased(client, worker, run_id))] == [(account, mine)]
    assert "api-token" not in leases  # the other member has no such secret: the agent's account is all there is


def test_an_account_of_the_agents_pool_goes_to_every_node_of_its_pool_never_the_dispatchers_secret(client, hub):
    """The worker an account of the agent is bound to does not matter once it is in the agent's account pool."""
    box, elsewhere = worker_of(client, hub["owner"], "box"), worker_of(client, hub["owner"], "elsewhere")
    added(client, hub["owner"], "claude-a", body(sample("agent-"), workers=["box"]))
    pooled(client, hub["owner"], "claude-a")
    env_secret(client, hub["owner"], "claude-own", VAR, sample("owner-"))
    run_id = run_under(client, hub["owner"], elsewhere)
    assert [lease["name"] for lease in for_var(leased(client, elsewhere, run_id))] == [lease_name("claude-a")]
    finish(client, elsewhere, run_id)
    run_id = run_under(client, hub["owner"], box)
    assert [lease["name"] for lease in for_var(leased(client, box, run_id))] == [lease_name("claude-a")]


def test_the_agents_accounts_rotate_and_a_run_keeps_its_account(client, hub, hub_db):
    worker = worker_of(client, hub["owner"], "box")
    values = {name: sample(f"{name}-") for name in ("claude-a", "claude-b")}
    for name, value in values.items():
        added(client, hub["owner"], name, body(value, workers=["box"]))
    pooled(client, hub["owner"], *values)
    env_secret(client, hub["owner"], "claude-own", VAR, sample("owner-"))
    taken = []
    for _ in range(4):
        run_id = run_under(client, hub["owner"], worker)
        (lease,) = for_var(leased(client, worker, run_id))
        assert for_var(leased(client, worker, run_id)) == [lease]  # a run keeps its account
        taken.append(lease["name"])
        finish(client, worker, run_id)
    assert sorted(taken[:2]) == [lease_name("claude-a"), lease_name("claude-b")] and taken[:2] == taken[2:]


def test_priority_pause_and_cooldown_choose_among_the_agents_accounts(client, hub, hub_db):
    worker = worker_of(client, hub["owner"], "box")
    for name in ("claude-a", "claude-b"):
        added(client, hub["owner"], name, body(sample(f"{name}-"), workers=["box"]))
    pooled(client, hub["owner"], "claude-a", "claude-b")
    env_secret(client, hub["owner"], "claude-own", VAR, sample("owner-"))
    resumed = client.patch(path("claude-a"), json={"paused": True}, headers=hub["owner"])
    assert resumed.status_code == 200, resumed.text
    for _ in range(2):  # a paused account is skipped
        run_id = run_under(client, hub["owner"], worker)
        assert [lease["name"] for lease in for_var(leased(client, worker, run_id))] == [lease_name("claude-b")]
        finish(client, worker, run_id)
    assert client.patch(path("claude-a"), json={"paused": False}, headers=hub["owner"]).status_code == 200
    cool(hub_db, "claude-b", 30)  # b cools down: a is the one ready
    run_id = run_under(client, hub["owner"], worker)
    assert [lease["name"] for lease in for_var(leased(client, worker, run_id))] == [lease_name("claude-a")]
    finish(client, worker, run_id)


def test_every_account_of_the_agent_unusable_leaves_the_variable_unleased_not_the_dispatchers(client, hub, hub_db):
    """Each time once the run was claimed: with no account of the pool usable, a new run waits in the queue
    (``test_account_pools``)."""
    worker = worker_of(client, hub["owner"], "box")
    for name in ("claude-a", "claude-b"):
        added(client, hub["owner"], name, body(sample(f"{name}-"), workers=["box"]))
    pooled(client, hub["owner"], "claude-a", "claude-b")
    theirs = sample("owner-")
    env_secret(client, hub["owner"], "claude-own", VAR, theirs)

    # every one cooling: the one whose cooldown ends first, with an event that names secrets by id
    run_id = run_under(client, hub["owner"], worker)
    cool(hub_db, "claude-a", 20)
    cool(hub_db, "claude-b", 10)
    assert [lease["name"] for lease in for_var(leased(client, worker, run_id))] == [lease_name("claude-b")]
    (event,) = events(hub_db, run_id)
    assert "cooling down" in event["text"] and "claude-" not in event["text"]
    finish(client, worker, run_id)

    # every one paused: none, and an event says it
    sql(hub_db, update(tables.accounts).values(cooldown_until=None))  # the cooldowns are over
    run_id = run_under(client, hub["owner"], worker)
    for name in ("claude-a", "claude-b"):
        assert client.patch(path(name), json={"paused": True}, headers=hub["owner"]).status_code == 200
    assert for_var(leased(client, worker, run_id)) == []
    assert any("paused" in event["text"] for event in events(hub_db, run_id))
    finish(client, worker, run_id)

    # every one past its end: none, and an event says it
    for name in ("claude-a", "claude-b"):
        assert client.patch(path(name), json={"paused": False}, headers=hub["owner"]).status_code == 200
    run_id = run_under(client, hub["owner"], worker)
    s = tables.secrets
    sql(hub_db, update(s).values(expires_at=s.c.created_at).where(s.c.profile_id.is_not(None)))
    assert for_var(leased(client, worker, run_id)) == []
    (event,) = events(hub_db, run_id)
    assert "ended" in event["text"] and "@auditor" in event["text"] and theirs not in event["text"]


def test_a_failover_moves_among_the_agents_accounts_alone(client, hub, hub_db):
    worker = worker_of(client, hub["owner"], "box")
    first, second = sample("a-"), sample("b-")
    added(client, hub["owner"], "claude-a", body(first, workers=["box"], priority=0))
    added(client, hub["owner"], "claude-b", body(second, workers=["box"], priority=1))
    pooled(client, hub["owner"], "claude-a", "claude-b")
    env_secret(client, hub["owner"], "claude-own", VAR, sample("owner-"))
    run_id = run_under(client, hub["owner"], worker)
    (held,) = for_var(leased(client, worker, run_id))
    limited = {"lease_id": held["id"], "status": "rejected", "rate_limit_type": "five_hour"}
    answer = client.post(f"/v1/worker/runs/{run_id}/rate-limit", json=limited, headers=worker["headers"])
    assert answer.status_code == 200, answer.text
    assert answer.json()["action"] == "failover"
    (moved,) = for_var(leased(client, worker, run_id))
    assert moved["name"] != held["name"] and moved["name"].startswith(f"@pool.{POOL}:")
    # the account it moved to is limited as well: the dispatcher's secret is no way out, the run stops
    limited = {"lease_id": moved["id"], "status": "rejected", "rate_limit_type": "five_hour"}
    answer = client.post(f"/v1/worker/runs/{run_id}/rate-limit", json=limited, headers=worker["headers"])
    assert answer.status_code == 200, answer.text
    assert answer.json()["action"] == "stop"


def test_an_account_picked_by_hand_for_a_variable_of_the_agent_is_refused_naming_it(client, hub):
    worker = worker_of(client, hub["owner"], "box")
    added(client, hub["owner"], "claude-a", body(sample("agent-"), workers=["box"]))
    pooled(client, hub["owner"], "claude-a")
    env_secret(client, hub["owner"], "claude-own", VAR, sample("owner-"))
    env_secret(client, hub["owner"], "api-token", OTHER_VAR, sample("api-"))
    request = {"plan_id": PLAN, "steps": [STEPS["evo-agents"]], "profile": "auditor"}
    refused = client.post(
        f"/v1/projects/{PROJECT}/runs", json={**request, "account": "claude-own"}, headers=hub["owner"]
    )
    assert refused.status_code == 422, refused.text
    assert "@auditor" in refused.json()["message"] and VAR in refused.json()["message"]
    # a variable the agent has no account for may be picked by hand
    ok = client.post(f"/v1/projects/{PROJECT}/runs", json={**request, "account": "api-token"}, headers=hub["owner"])
    assert ok.status_code == 201, ok.text
    run_id = ok.json()[0]["id"]
    assert client.post("/v1/worker/claim", json={"wait_s": 0}, headers=worker["headers"]).json()["run"]["id"] == run_id
    finish(client, worker, run_id)
    rerun_refused = client.post(
        f"/v1/projects/{PROJECT}/runs/{run_id}/rerun", json={"account": "claude-own"}, headers=hub["owner"]
    )
    assert rerun_refused.status_code == 422, rerun_refused.text
    assert "@auditor" in rerun_refused.json()["message"]


def test_the_values_of_the_agents_accounts_are_masked_in_what_it_posts(client, hub, hub_db):
    worker_of(client, hub["owner"], "box")
    first, second = sample("agent-"), sample("agent-longer-")
    added(client, hub["owner"], "claude-a", body(first, workers=["box"]))
    added(client, hub["owner"], "claude-b", body(second, workers=["box"]))
    state = client.app.state
    pid = profile_id(hub_db)

    async def mask(text: str, profile: int = pid) -> str:
        async with state.engine.begin() as conn:
            return await mask_values(conn, state.sealer, profile, text)

    said = f"token {first}, then {second}; and {first}{second}"
    assert client.portal.call(mask, said) == "token ***, then ***; and ******"
    assert client.portal.call(mask, "nothing to hide") == "nothing to hide"
    assert client.portal.call(mask, said, pid + 1000) == said  # another profile's text is left alone
    deleted = client.delete(path("claude-a"), headers=hub["owner"])
    assert deleted.status_code in (200, 204), deleted.text
    assert client.portal.call(mask, f"{first} {second}") == f"{first} ***"
