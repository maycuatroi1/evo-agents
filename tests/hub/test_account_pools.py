"""Account pools, step 3 of the plan agent-dedicated-pools (criteria a4 and a7).

a4: project admins and hub admins make named account pools, put env and provider accounts in them and attach one to an
agent (an agent has one account pool; a pool serves many agents; a writer may do none of it); a run of an agent in pool
mode leases the accounts of its pool alone, never the dispatcher's secret for a model variable, and waits in the queue
when no account of the pool is usable; dispatching or mentioning an agent in pool mode with no account pool is refused
with a message that tells an admin to attach one.
a7: a run of the Curator or a local run is not held by a pool. Step 8 removed the legacy mode, so every agent is in pool mode.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select, update

from evo_agents.hub import runs, tables
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import sql
from tests.hub.test_agent_pool_placement import (  # noqa: F401  (fixtures and helpers of the placement tests)
    chat_client,
    chat_hub,
    doctor,
    pool_id,
)
from tests.hub.test_chat_runs import created, sent
from tests.hub.test_credentials_api import PLAN, PROJECT, STEPS, by_name, env_secret, leased, sample
from tests.hub.test_credentials_api import worker_of as plain_worker
from tests.hub.test_profile_runs import beat

app_key, config, client = credentials_api.app_key, credentials_api.config, credentials_api.client

VAR = "CLAUDE_CODE_OAUTH_TOKEN"
ADMIN_GRANT = {"role": "admin", "max_level": "internal"}
POOLS = f"/v1/projects/{PROJECT}/account-pools"


@pytest.fixture
def hub(client, github) -> dict:
    """The members of ``test_credentials_api`` with the owner an admin of the project, the other member a writer and
    the agent @auditor (in pool mode)."""
    headers = credentials_api.members(client, github)
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/owner", json=ADMIN_GRANT, headers=headers["admin"])
    assert granted.status_code == 200, granted.text
    writer = client.put(
        f"/v1/admin/projects/{PROJECT}/grants/{credentials_api.ADMIN}",
        json=credentials_api.WRITER,
        headers=headers["admin"],
    )  # the hub admin keeps accounts of their own, which needs a role on the project
    assert writer.status_code == 200, writer.text
    made = client.post(
        f"/v1/projects/{PROJECT}/agents", json={"handle": "auditor", "name": "Auditor"}, headers=headers["owner"]
    )
    assert made.status_code == 201, made.text
    assert made.json()["account_pool"] is None and "placement" not in made.json()
    return headers


def make_pool(client, headers, name: str = "main", *accounts: str):
    response = client.post(POOLS, json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    for account in accounts:
        added = client.put(f"{POOLS}/{name}/accounts", json={"name": account}, headers=headers)
        assert added.status_code == 200, added.text
    return response.json()


def attach(client, headers, pool: str | None, handle: str = "auditor"):
    return client.put(
        f"/v1/projects/{PROJECT}/agents/{handle}/account-pool", json={"account_pool": pool}, headers=headers
    )


def node(client, db, headers, name: str = "box", handle: str = "auditor") -> dict:
    """A worker of the member that is a ready node of the pool of agent ``handle``."""
    worker = plain_worker(client, headers, name)
    beat(client, worker, features=[runs.AGENT_HOME])
    sql(db, update(tables.workers).where(tables.workers.c.id == worker["id"]).values(pool_id=pool_id(db, handle)))
    doctor(client, worker)
    return worker


def dispatch(client, headers, **extra):
    request = {"plan_id": PLAN, "steps": [STEPS["evo-agents"]], "profile": "auditor", **extra}
    return client.post(f"/v1/projects/{PROJECT}/runs", json=request, headers=headers)


def claim(client, worker: dict):
    return client.post("/v1/worker/claim", json={"wait_s": 0}, headers=worker["headers"]).json()["run"]


def test_admins_make_pools_add_accounts_and_attach_one_but_a_writer_may_do_none_of_it(client, hub, hub_db):
    env_secret(client, hub["owner"], "claude-a", VAR, sample("a-"))
    env_secret(client, hub["admin"], "claude-b", VAR, sample("b-"))
    assert client.post(POOLS, json={"name": "main"}, headers=hub["other"]).status_code == 403
    assert client.get(POOLS, headers=hub["other"]).status_code == 403
    make_pool(client, hub["owner"], "main", "claude-a")  # an admin of the project
    assert (
        client.post(POOLS, json={"name": "MAIN"}, headers=hub["owner"]).status_code == 409
    )  # a name once, in any case
    assert client.put(f"{POOLS}/main/accounts", json={"name": "claude-a"}, headers=hub["other"]).status_code == 403
    shown = client.get(f"{POOLS}/main", headers=hub["owner"]).json()
    assert [(a["name"], a["kind"], a["env_var"]) for a in shown["accounts"]] == [("claude-a", "env", VAR)]
    assert shown["agents"] == []

    # a hub admin makes one and puts an account of their own in it
    made = client.post(POOLS, json={"name": "spare"}, headers=hub["admin"])
    assert made.status_code == 201, made.text
    added = client.put(f"{POOLS}/spare/accounts", json={"name": "claude-b"}, headers=hub["admin"])
    assert added.status_code == 200 and [a["name"] for a in added.json()["accounts"]] == ["claude-b"]
    assert client.put(f"{POOLS}/main/accounts", json={"name": "nope"}, headers=hub["owner"]).status_code == 404

    assert attach(client, hub["other"], "main").status_code == 403  # a writer
    attached = attach(client, hub["owner"], "main")
    assert attached.status_code == 200 and attached.json()["account_pool"] == "main"
    assert attach(client, hub["admin"], "spare").json()["account_pool"] == "spare"  # one pool for an agent: replaced
    assert attach(client, hub["owner"], "main").json()["account_pool"] == "main"
    assert attach(client, hub["owner"], "absent").status_code == 404
    assert client.get(f"{POOLS}/main", headers=hub["owner"]).json()["agents"] == ["auditor"]
    assert client.delete(f"{POOLS}/main", headers=hub["owner"]).status_code == 409  # an agent uses it
    assert attach(client, hub["owner"], None).json()["account_pool"] is None
    removed = client.delete(f"{POOLS}/main/accounts/{shown['accounts'][0]['id']}", headers=hub["owner"])
    assert removed.status_code == 200 and removed.json()["accounts"] == []
    assert client.delete(f"{POOLS}/main", headers=hub["owner"]).status_code == 204
    kinds = {
        row[0] for row in sql(hub_db, select(tables.audit.c.action).where(tables.audit.c.action.like("account-pool.%")))
    }
    assert kinds == {
        "account-pool.create",
        "account-pool.add",
        "account-pool.attach",
        "account-pool.remove",
        "account-pool.delete",
    }


def test_a_run_of_an_agent_in_pool_mode_leases_the_pools_account_never_the_dispatchers_secret(client, hub, hub_db):
    mine, pooled = sample("owner-"), sample("pool-")
    env_secret(client, hub["owner"], "dispatchers", VAR, mine)  # the dispatcher's own Claude token
    env_secret(client, hub["admin"], "pooled", VAR, pooled)
    make_pool(client, hub["admin"], "main", "pooled")
    assert attach(client, hub["admin"], "main").status_code == 200
    worker = node(client, hub_db, hub["owner"])
    run_id = dispatch(client, hub["owner"]).json()[0]["id"]
    spec = claim(client, worker)
    assert spec["id"] == run_id and spec["account_pool"] is True and spec["own_home"] is True
    assert "owner_setup" not in spec, "@auditor is not the project's default agent: none of the owner's setup"
    leases = by_name(leased(client, worker, run_id))
    assert [(lease["name"], lease["env_var"], lease["value"]) for lease in leases.values()] == [
        ("@pool.main:pooled", VAR, pooled)
    ]
    assert mine not in str(leases)


def test_with_no_usable_account_in_the_pool_the_run_waits_in_the_queue_and_takes_nothing_else(client, hub, hub_db):
    env_secret(client, hub["owner"], "dispatchers", VAR, sample("owner-"))
    env_secret(client, hub["admin"], "pooled", VAR, sample("pool-"))
    make_pool(client, hub["admin"], "main", "pooled")
    assert attach(client, hub["admin"], "main").status_code == 200
    worker = node(client, hub_db, hub["owner"])
    run_id = dispatch(client, hub["owner"]).json()[0]["id"]
    s = tables.secrets
    sql(hub_db, update(s).where(s.c.name == "pooled").values(expires_at=s.c.created_at))  # past its end
    assert client.post("/v1/worker/claim", json={"wait_s": 0}, headers=worker["headers"]).json()["run"] is None
    r = tables.runs
    assert sql(hub_db, select(r.c.state).where(r.c.id == run_id)) == [("queued",)]
    sql(hub_db, update(s).where(s.c.name == "pooled").values(expires_at=None))
    assert claim(client, worker)["id"] == run_id


def test_dispatching_or_mentioning_an_agent_in_pool_mode_with_no_account_pool_is_refused_for_an_admin_to_fix(
    client, hub, hub_db
):
    response = dispatch(client, hub["owner"])
    assert response.status_code == 422
    assert "agent @auditor has no account pool" in response.json()["message"]
    assert "ask an admin" in response.json()["message"] and "account-pool auditor" in response.json()["message"]
    assert sql(hub_db, select(tables.runs.c.id)) == []


def test_a_mention_of_an_agent_in_pool_mode_with_no_account_pool_is_answered_with_the_refusal(
    chat_client, chat_hub, hub_db
):
    client, hub = chat_client, chat_hub
    p = tables.agent_profiles
    sql(hub_db, update(p).where(p.c.handle == "reviewer").values(account_pool_id=None))
    chat = created(client, hub["owner"])
    mention = sent(client, hub["owner"], chat["id"], "@reviewer please look")
    (refusal,) = [m["refusal"] for m in mention["mentions"]]
    assert refusal.startswith("@reviewer: ") and "has no account pool" in refusal and "ask an admin" in refusal
    assert sql(hub_db, select(tables.runs.c.id).where(tables.runs.c.kind == "chat")) == []
