"""A night shift on a pool, and secrets and accounts bound to a pool, step 3 of the worker-pools plan (criterion a4, and
a9 for what stays as it was).

a4: a charter names ``pool`` or ``worker``, exactly one; the night shift places the runs of a pool's schedule with the
selector pool=<name> instead of pinning them, so any ready node of the pool claims them; ``build_now`` and the brief
say when the pool has no ready node. A secret or an account bound to a pool reaches each node of it, those that join
after the binding too. A charter, schedule or binding naming a worker keeps its behaviour.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import delete, select, update

from evo_agents.hub import tables
from evo_agents.hub.cli_bindings import BOTH, bound_to, where_cell, where_text
from evo_agents.hub.server.brief import _pool, _pool_words
from evo_agents.hub.server.night_duty import on_duty
from tests.hub.live import sql
from tests.hub.test_credentials_api import (  # noqa: F401  (app_key, client and hub are fixtures)
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
    audit_of,
    charter_body,
    count,
    fire,
    night,  # noqa: F401  (a fixture)
    put_charter,
    scheduled,
    shown,
    user_id,
    written,
)
from tests.hub.test_curator_changes import accepted, build_now, curator_charter, protect, world  # noqa: F401
from tests.hub.test_plan_runs import fleet_worker
from tests.hub.test_pool_scheduling import doctor, pool, run_view
from tests.hub.test_run_cli import HUB_URL, write_credentials
from tests.hub.test_runs import PROJECT, claim, moved, web_client  # noqa: F401  (fixtures)
from tests.hub.test_secrets_cli import run_cli


def fleet_node(client, headers, db, name: str, of_pool: dict) -> dict:
    """A worker of ``headers``' member with a checkout of the plan run's repos, made a node of ``of_pool``."""
    worker = fleet_worker(client, headers, name)
    sql(db, update(tables.workers).where(tables.workers.c.id == worker["id"]).values(pool_id=of_pool["id"]))
    return worker


def pool_charter(**changes) -> dict:
    return charter_body(worker=None, pool="ci", **changes)


def schedule_of(db) -> tuple:
    s = tables.schedules
    return sql(db, select(s.c.worker_id, s.c.pool_id, s.c.owner_id))[0]


def due_of(client, db) -> SimpleNamespace:
    s = tables.schedules
    (row,) = sql(db, select(s.c.id, s.c.project_id, s.c.worker_id, s.c.pool_id))
    return SimpleNamespace(id=row[0], project_id=row[1], worker_id=row[2], pool_id=row[3])


def called(client, fn, *args):
    async def run():
        async with client.app.state.engine.connect() as conn:
            return await fn(conn, *args)

    return client.portal.call(run)


# The charter


def test_a_charter_names_a_pool_of_its_writer_and_its_schedule_follows_the_pool(night, hub_db):
    client, headers = night.client, night.headers
    ci = pool(client, headers["owner"])
    written(client, headers["owner"], pool_charter())
    held = shown(client, headers["owner"])
    assert (held["pool"], held["pool_id"], held["worker"], held["worker_id"]) == ("ci", ci["id"], None, None)
    assert held["schedule_owner"] == OWNER
    assert schedule_of(hub_db) == (None, ci["id"], user_id(hub_db, OWNER))
    action, target, login, _ = audit_of(hub_db, "curator")[-1]
    assert (action, login, target) == ("curator.charter", OWNER, f"{PROJECT} revision=1 pool=ci")

    # back to the worker: the schedule follows it
    written(client, headers["owner"], charter_body())
    assert schedule_of(hub_db) == (night.worker["id"], None, user_id(hub_db, OWNER))
    assert audit_of(hub_db, "curator")[-1][1].endswith(f"worker={night.worker['name']}")


@pytest.mark.parametrize(
    "changes",
    [{"pool": "ci"}, {"worker": None}],
    ids=["both", "neither"],
)
def test_a_charter_names_the_worker_or_the_pool_exactly_one(night, hub_db, changes):
    client, headers = night.client, night.headers
    pool(client, headers["owner"])
    response = put_charter(client, headers["owner"], charter_body(**changes))
    assert response.status_code == 422, response.text
    assert "exactly one of the two" in response.text
    assert count(hub_db, tables.charters) == 0


def test_a_charter_names_no_pool_of_another_member_and_only_a_pool_that_serves_the_project(night, hub_db):
    client, headers = night.client, night.headers
    response = client.post("/v1/worker-pools", json={"name": "ci", "projects": [PROJECT]}, headers=headers["other"])
    assert response.status_code == 201, response.text
    refused = put_charter(client, headers["owner"], pool_charter())
    assert refused.status_code == 403 and "you have no pool ci" in refused.text
    mine = pool(client, headers["owner"])
    pp = tables.worker_pool_projects
    sql(hub_db, delete(pp).where(pp.c.pool_id == mine["id"]))
    unlisted = put_charter(client, headers["owner"], pool_charter())
    assert unlisted.status_code == 409 and f"does not serve project {PROJECT}" in unlisted.text
    assert count(hub_db, tables.charters) == 0


# The schedule


def test_the_night_shift_places_a_pool_run_by_its_selector_and_a_ready_node_claims_it(night, hub_db):
    client, headers = night.client, night.headers
    ci = pool(client, headers["owner"])
    lab = fleet_node(client, headers["owner"], hub_db, "lab", ci)
    ready = fleet_node(client, headers["owner"], hub_db, "ready", ci)
    doctor(client, ready)
    written(client, headers["owner"], pool_charter())

    assert fire(client, NIGHT) == {"queued": 1}
    (run,) = scheduled(hub_db)
    assert run["pinned_worker_id"] is None and run["dispatched_by"] == user_id(hub_db, OWNER)
    view = run_view(client, headers["owner"], run["id"])
    assert (view["selector"], view["tolerations"]) == (["pool=ci"], [])
    e = tables.run_events
    (note,) = sql(hub_db, select(e.c.body).where(e.c.run_id == run["id"], e.c.kind == "system"))
    assert "pool ci" in note[0]["text"]

    assert claim(client, night.worker) is None  # the owner's worker of no pool
    assert claim(client, lab) is None  # a node with no doctor report is not ready
    assert claim(client, ready)["id"] == run["id"]


def test_a_pool_runs_tolerate_the_pools_taints(night, hub_db):
    client, headers = night.client, night.headers
    ci = pool(client, headers["owner"], taints=["ci-only"])
    lab = fleet_node(client, headers["owner"], hub_db, "lab", ci)
    doctor(client, lab)
    written(client, headers["owner"], pool_charter())
    assert fire(client, NIGHT) == {"queued": 1}
    (run,) = scheduled(hub_db)
    assert run_view(client, headers["owner"], run["id"])["tolerations"] == ["ci-only"]
    assert claim(client, lab)["id"] == run["id"]


def test_a_pool_with_no_ready_node_queues_nothing_and_the_brief_says_why(night, hub_db):
    client, headers = night.client, night.headers
    ci = pool(client, headers["owner"])
    written(client, headers["owner"], pool_charter())
    assert fire(client, NIGHT) == {"pool": 1}
    due = due_of(client, hub_db)
    duty, why = called(client, on_duty, due)
    assert duty is None and why == "pool ci has no live node: add one with a join token"

    lab = fleet_node(client, headers["owner"], hub_db, "lab", ci)
    assert fire(client, NIGHT) == {"pool": 1}
    assert scheduled(hub_db) == []
    facts = called(client, _pool, due)
    assert (facts["name"], facts["nodes"], facts["ready"]) == ("ci", 1, 0)
    words = _pool_words(facts)
    assert words.startswith("Pool ci: no node ready for a run now: pool ci has no node ready for a run now (lab is")
    assert "sent no doctor report" in words

    doctor(client, lab)
    facts = called(client, _pool, due)
    assert (facts["ready"], _pool_words(facts)) == (1, "Pool ci: 1 of 1 node ready.")
    assert fire(client, NIGHT) == {"queued": 1}


def test_run_plan_on_a_curator_plan_says_when_the_pool_has_no_ready_node(world):
    accepted(world)
    assert protect(world).json()["protected"] is True
    ci = pool(world.client, world.headers["owner"])
    sql(world.db, update(tables.workers).where(tables.workers.c.id == world.worker["id"]).values(pool_id=ci["id"]))
    written(world.client, world.headers["owner"], {**curator_charter(), "worker": None, "pool": "ci"})
    refused = build_now(world)
    assert refused.status_code == 409, refused.text
    assert "pool ci has no node ready for a run now (mac-mini is not ready" in refused.text
    assert "nothing was queued" in refused.text


# Secrets and accounts bound to a pool


def ci_node(client, headers, db, name: str, of_pool: dict) -> dict:
    """A worker of ``headers``' member with the checkouts of the credentials plan, made a ready node of ``of_pool``."""
    worker = worker_of(client, headers, name)
    sql(db, update(tables.workers).where(tables.workers.c.id == worker["id"]).values(pool_id=of_pool["id"]))
    doctor(client, worker)
    return worker


def test_a_secret_bound_to_a_pool_reaches_each_node_of_it_those_that_join_later_too(client, hub, hub_db):  # noqa: F811
    ci = pool(client, hub["owner"])
    desk = worker_of(client, hub["owner"], "desk")
    env_secret(client, hub["owner"], "everywhere", "API_TOKEN", sample("everywhere-"))
    ci_value = sample("ci-")
    env_secret(client, hub["owner"], "ci-api", "API_TOKEN", ci_value, pools=["CI"])
    env_secret(client, hub["owner"], "ci-only", "CI_TOKEN", sample("ci-only-"), pools=["ci"])
    (listed,) = [item for item in client.get("/v1/secrets", headers=hub["owner"]).json() if item["name"] == "ci-api"]
    assert (listed["workers"], listed["pools"]) == ([], ["ci"])
    b = tables.secret_bindings
    assert sql(hub_db, select(b.c.worker_id, b.c.pool_id).where(b.c.secret_id == secret_id_of(hub_db, "ci-api"))) == [
        (None, ci["id"])
    ]

    lab = ci_node(client, hub["owner"], hub_db, "lab", ci)  # it joins after the bindings
    on_lab = step_run(client, hub["owner"], "notes", lab)
    lab_leases = by_name(leased(client, lab, on_lab))
    # on a node of the pool a secret bound to the pool wins over one of the same variable bound to any worker
    assert set(lab_leases) == {"ci-api", "ci-only"} and lab_leases["ci-api"]["value"] == ci_value
    on_desk = step_run(client, hub["owner"], "lumen-kb-docs", desk)
    assert set(by_name(leased(client, desk, on_desk))) == {"everywhere"}


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


def test_an_account_bound_to_a_pool_goes_with_a_run_sent_to_the_pool_or_pinned_to_a_node_of_it(client, hub, hub_db):  # noqa: F811
    ci = pool(client, hub["owner"])
    lab = ci_node(client, hub["owner"], hub_db, "lab", ci)
    desk = worker_of(client, hub["owner"], "desk")
    env_secret(client, hub["owner"], "ci-claude", "CLAUDE_CODE_OAUTH_TOKEN", sample(), pools=["ci"])
    (account,) = [a for a in client.get("/v1/accounts", headers=hub["owner"]).json() if a["name"] == "ci-claude"]
    assert (account["workers"], account["pools"]) == ([], ["ci"])

    def sent(**extra):
        body = {"plan_id": "credentials-smoke", "steps": [STEPS["notes"]], "account": "ci-claude", **extra}
        return client.post(f"/v1/projects/{PROJECT}/runs", json=body, headers=hub["owner"])

    loose = sent()
    assert loose.status_code == 422 and "on pool ci only" in loose.text
    elsewhere = sent(worker_id=desk["id"])
    assert elsewhere.status_code == 422 and "not on worker desk" in elsewhere.text
    to_pool = sent(pool="ci")
    assert to_pool.status_code == 201, to_pool.text
    assert claim(client, desk) is None
    assert claim(client, lab)["id"] == to_pool.json()[0]["id"]
    moved(client, lab, to_pool.json()[0]["id"], "running", "failed", error="it stopped", failure_cause="verify_failed")
    pinned = sent(worker_id=lab["id"])
    assert pinned.status_code == 201, pinned.text


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
