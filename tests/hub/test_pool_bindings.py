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
from evo_agents.hub.server.brief import _pool, _pool_words
from evo_agents.hub.server.night_duty import on_duty
from tests.hub.live import sql
from tests.hub.test_credentials_api import app_key  # noqa: F401  (a fixture)
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
from tests.hub.test_runs import PROJECT, claim, web_client  # noqa: F401  (fixtures)


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
