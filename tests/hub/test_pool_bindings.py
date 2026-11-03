"""A night shift on a pool, and secrets and accounts bound to a pool, step 3 of the worker-pools plan (criterion a4, and
a9 for what stays as it was), in the pools of the agents (plan agent-dedicated-pools).

a4: the night shift places its runs on the pool of the project's default agent with the selector pool=<name>, pinning
none, so any ready node of the pool claims them; ``build_now`` and the brief say when the pool has no ready node. A
charter names no worker or pool any more (``tests.hub.test_curator``). A secret or an account bound to a pool reaches
each node of it, those that join after the binding too.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select, update

from evo_agents.hub import tables
from evo_agents.hub.cli_bindings import BOTH, bound_to, where_cell, where_text
from evo_agents.hub.server.brief import _pool, _pool_words
from evo_agents.hub.server.night_duty import on_duty
from tests.hub.account_pool_setup import give_account_pool
from tests.hub.live import sql
from tests.hub.test_credentials_api import (  # noqa: F401  (app_key, client and hub are fixtures)
    CHECKOUTS,
    STEPS,
    app_key,
    by_name,
    client,
    config,
    env_secret,
    hub,
    leased,
    sample,
    step_run,
    worker_of,
)
from tests.hub.test_curator import (
    NIGHT,
    OWNER,
    charter_body,
    count,
    fire,
    night,  # noqa: F401  (a fixture)
    scheduled,
    user_id,
    written,
)
from tests.hub.test_curator_changes import accepted, build_now, protect, world  # noqa: F401
from tests.hub.test_plan_runs import fleet_worker
from tests.hub.test_pool_scheduling import HIGH, agentless, doctor, pool, run_view
from tests.hub.test_run_cli import HUB_URL, write_credentials
from tests.hub.test_runs import PROJECT, add_worker, claim, dispatched, web_client  # noqa: F401  (fixtures)
from tests.hub.test_secrets_cli import run_cli

HOME = f"agent-{PROJECT}"  # the pool of the project's default agent, where the night shift runs


def home_id(db) -> int:
    p = tables.worker_pools
    return sql(db, select(p.c.id).where(p.c.name == HOME))[0][0]


def fleet_node(client, headers, db, name: str, of_pool: dict) -> dict:
    """A worker of ``headers``' member with a checkout of the plan run's repos, made a node of ``of_pool`` with no
    doctor report yet: not ready."""
    worker = fleet_worker(client, headers, name, pool=None)
    sql(db, update(tables.workers).where(tables.workers.c.id == worker["id"]).values(pool_id=of_pool["id"]))
    return worker


def due_of(client, db) -> SimpleNamespace:
    s = tables.schedules
    (row,) = sql(db, select(s.c.id, s.c.project_id))
    return SimpleNamespace(id=row[0], project_id=row[1])


def called(client, fn, *args):
    async def run():
        async with client.app.state.engine.connect() as conn:
            return await fn(conn, *args)

    return client.portal.call(run)


# The schedule


def test_the_night_shift_places_its_run_on_the_default_agents_pool_and_a_ready_node_claims_it(night, hub_db):
    client, headers = night.client, night.headers
    lab = fleet_node(client, headers["owner"], hub_db, "lab", {"id": home_id(hub_db)})
    stranger = fleet_worker(client, headers["owner"], "stranger", pool=None)
    written(client, headers["owner"], charter_body())

    assert fire(client, NIGHT) == {"queued": 1}
    (run,) = scheduled(hub_db)
    assert run["pinned_worker_id"] is None and run["dispatched_by"] == user_id(hub_db, OWNER)
    view = run_view(client, headers["owner"], run["id"])
    assert (view["selector"], view["tolerations"]) == ([f"pool={HOME}"], [])
    e = tables.run_events
    (note,) = sql(hub_db, select(e.c.body).where(e.c.run_id == run["id"], e.c.kind == "system"))
    assert f"pool {HOME}" in note[0]["text"]

    assert claim(client, stranger) is None  # the owner's worker of no pool
    assert claim(client, lab) is None  # a node with no doctor report is not ready
    assert claim(client, night.worker)["id"] == run["id"]


def test_a_pool_runs_tolerate_the_pools_taints(night, hub_db):
    client, headers = night.client, night.headers
    tainted = client.patch(f"/v1/worker-pools/{HOME}", json={"taints": ["ci-only"]}, headers=headers["owner"])
    assert tainted.status_code == 200, tainted.text
    written(client, headers["owner"], charter_body())
    assert fire(client, NIGHT) == {"queued": 1}
    (run,) = scheduled(hub_db)
    assert run_view(client, headers["owner"], run["id"])["tolerations"] == ["ci-only"]
    assert claim(client, night.worker)["id"] == run["id"]


def test_a_pool_with_no_ready_node_queues_nothing_and_the_brief_says_why(night, hub_db):
    client, headers = night.client, night.headers
    w = tables.workers
    sql(hub_db, update(w).values(pool_id=None).where(w.c.id == night.worker["id"]))  # the pool's one node leaves it
    written(client, headers["owner"], charter_body())
    assert fire(client, NIGHT) == {"pool": 1}
    due = due_of(client, hub_db)
    duty, why = called(client, on_duty, due)
    assert duty is None and why == f"pool {HOME} has no live node: add one with a join token"

    lab = fleet_node(client, headers["owner"], hub_db, "lab", {"id": home_id(hub_db)})
    assert fire(client, NIGHT) == {"pool": 1}
    assert scheduled(hub_db) == []
    facts = called(client, _pool, due)
    assert (facts["name"], facts["nodes"], facts["ready"]) == (HOME, 1, 0)
    words = _pool_words(facts)
    assert words.startswith(
        f"Pool {HOME}: no node ready for a run now: pool {HOME} has no node ready for a run now (lab is"
    )
    assert "sent no doctor report" in words

    doctor(client, lab)
    facts = called(client, _pool, due)
    assert (facts["ready"], _pool_words(facts)) == (1, f"Pool {HOME}: 1 of 1 node ready.")
    assert fire(client, NIGHT) == {"queued": 1}


def test_run_plan_on_a_curator_plan_says_when_the_pool_has_no_ready_node(world):
    accepted(world)
    assert protect(world).json()["protected"] is True
    doctor(world.client, world.worker, HIGH)  # the one node of the default agent's pool is not ready
    refused = build_now(world)
    assert refused.status_code == 409, refused.text
    assert f"pool {HOME} has no node ready for a run now (mac-mini is not ready" in refused.text
    assert "nothing was queued" in refused.text


# Secrets and accounts bound to a pool


def claimable(db) -> None:
    """The project's default agent with an account pool, which a dispatch of its runs needs, unless it has one."""
    p, ap = tables.projects, tables.agent_profiles
    held = select(ap.c.account_pool_id).join_from(ap, p, p.c.id == ap.c.project_id)
    if sql(db, held.where(p.c.name == PROJECT, ap.c.handle == PROJECT))[0][0] is None:
        give_account_pool(db, PROJECT, PROJECT, OWNER)


def owned_home(db) -> int:
    """The pool of the project's default agent made the owner's, as if owner had made the agent, so that owner binds
    secrets to it; its id."""
    pools = tables.worker_pools
    sql(db, update(pools).values(owner_id=user_id(db, OWNER)).where(pools.c.name == HOME))
    return home_id(db)


def test_a_secret_bound_to_a_pool_reaches_each_node_of_it_those_that_join_later_too(client, hub, hub_db):  # noqa: F811
    claimable(hub_db)
    home = owned_home(hub_db)
    desk = add_worker(client, hub["owner"], "desk", checkouts=CHECKOUTS, pool=None)
    env_secret(client, hub["owner"], "everywhere", "API_TOKEN", sample("everywhere-"))
    ci_value = sample("ci-")
    env_secret(client, hub["owner"], "ci-api", "API_TOKEN", ci_value, pools=[HOME.upper()])
    env_secret(client, hub["owner"], "ci-only", "CI_TOKEN", sample("ci-only-"), pools=[HOME])
    (listed,) = [item for item in client.get("/v1/secrets", headers=hub["owner"]).json() if item["name"] == "ci-api"]
    assert (listed["workers"], listed["pools"]) == ([], [HOME])
    b = tables.secret_bindings
    assert sql(hub_db, select(b.c.worker_id, b.c.pool_id).where(b.c.secret_id == secret_id_of(hub_db, "ci-api"))) == [
        (None, home)
    ]

    lab = worker_of(client, hub["owner"], "lab")  # it joins the pool after the bindings
    on_lab = step_run(client, hub["owner"], "notes", lab)
    lab_leases = by_name(leased(client, lab, on_lab))
    # on a node of the pool a secret bound to the pool wins over one of the same variable bound to any worker
    assert set(lab_leases) == {"ci-api", "ci-only"} and lab_leases["ci-api"]["value"] == ci_value
    # a worker of no pool takes a run of no agent alone (one from before agents): the pool's secrets stay off it
    (run,) = dispatched(client, hub["owner"], [STEPS["lumen-kb-docs"]], plan_id="credentials-smoke")
    agentless(hub_db, run["id"])
    assert claim(client, desk)["id"] == run["id"]
    assert set(by_name(leased(client, desk, run["id"]))) == {"everywhere"}


def secret_id_of(db, name: str) -> int:
    s = tables.secrets
    return sql(db, select(s.c.id).where(s.c.name == name, s.c.deleted_at.is_(None)))[0][0]


def test_a_secret_binds_to_the_callers_own_pools_and_never_to_workers_and_pools_both(client, hub, hub_db):  # noqa: F811
    pool(client, hub["owner"])
    worker_of(client, hub["owner"], "desk")
    body = {"projects": [PROJECT], "kind": "env", "env_var": "API_TOKEN", "value": sample()}
    both = client.put("/v1/secrets/both", json={**body, "workers": ["desk"], "pools": ["ci"]}, headers=hub["owner"])
    assert both.status_code == 422 and "not both" in both.text
    theirs = client.put("/v1/secrets/theirs", json={**body, "pools": ["ci"]}, headers=hub["other"])
    assert theirs.status_code == 403 and "ci is not a worker pool of yours" in theirs.text
    forge = {"kind": "forge", "url_prefix": "https://gitlab.example.org/ops", "pools": ["ci"]}
    forge["expires_at"] = "2099-01-01T00:00:00Z"
    refused = client.put("/v1/secrets/forge", json={**body, **forge, "env_var": None}, headers=hub["owner"])
    assert refused.status_code == 422 and "binds to no worker or pool" in refused.text
    assert count(hub_db, tables.secret_bindings) == 0


def test_an_account_bound_to_a_pool_goes_with_no_run_of_an_agent_elsewhere(client, hub, hub_db):  # noqa: F811
    """A run of an agent goes to the nodes of the agent's own pool, and its dispatch names no worker or pool: an
    account bound to another pool goes with none of its runs."""
    claimable(hub_db)
    pool(client, hub["owner"])
    desk = add_worker(client, hub["owner"], "desk", checkouts=CHECKOUTS, pool=None)
    env_secret(client, hub["owner"], "ci-claude", "CLAUDE_CODE_OAUTH_TOKEN", sample(), pools=["ci"])
    (account,) = [a for a in client.get("/v1/accounts", headers=hub["owner"]).json() if a["name"] == "ci-claude"]
    assert (account["workers"], account["pools"]) == ([], ["ci"])

    def sent(**extra):
        body = {"plan_id": "credentials-smoke", "steps": [STEPS["notes"]], "account": "ci-claude", **extra}
        return client.post(f"/v1/projects/{PROJECT}/runs", json=body, headers=hub["owner"])

    loose = sent()
    assert loose.status_code == 422 and "on pool ci only" in loose.text
    for placed in ({"pool": "ci"}, {"worker_id": desk["id"]}):
        refused = sent(**placed)
        assert refused.status_code == 422 and "agent-<handle>" in refused.text, (placed, refused.text)
    assert count(hub_db, tables.runs) == 0


# The command line


@pytest.mark.parametrize(
    "args, problem",
    [
        (("--kind", "env", "--env-var", "API", "--worker", "mac", "--pool", "ci"), BOTH),
        (("--kind", "forge", "--url-prefix", "https://h/g", "--pool", "ci"), "takes no --worker or --pool"),
    ],
)
def test_secret_set_refuses_a_pool_with_workers_or_on_a_forge_secret_before_the_value_is_read(
    monkeypatch, capsys, tmp_path, args, problem
):
    home = write_credentials(tmp_path / "home", HUB_URL, "owner", "evh_unused")
    result = run_cli(monkeypatch, capsys, home, "hub", "secret", "set", "n", *args, "--project", "demo")
    assert result.code == 2 and result.out == ""
    assert problem in result.err


def test_the_command_line_says_where_a_secret_or_account_is_bound():
    on_pool = {"workers": [], "pools": ["ci"]}
    assert (where_text(on_pool), where_cell(on_pool)) == ("on pool ci", "pool ci")
    on_worker = {"workers": ["mac"], "pools": []}
    assert (where_text(on_worker), where_cell(on_worker)) == ("on mac", "mac")
    anywhere = {"workers": []}  # an answer of a hub before pools
    assert (where_text(anywhere), where_cell(anywhere)) == ("on any worker of yours", "any")
    account = {"workers": [], "pools": ["ci"]}
    renewed = SimpleNamespace(worker=None, pool=None)
    assert bound_to(renewed, account) == {"workers": [], "pools": ["ci"]}  # renewing keeps them
    assert bound_to(SimpleNamespace(worker=["mac"], pool=None), account) == {"workers": ["mac"], "pools": []}
    with pytest.raises(ValueError, match="not both"):
        bound_to(SimpleNamespace(worker=["mac"], pool=["ci"]), None)
