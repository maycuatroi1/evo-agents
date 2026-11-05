"""The shared KG pool, step 1 of plan kg-zip-to-answers (criterion a1, a7 in part).

a1: a hub admin names one pool of a member the shared KG pool. The KG runs (lens kg_questions and kg_init) of a project
with no repo, whose default agent's pool has no ready node, go to a ready node of the shared pool when the project's
creator owns it; the hub adds the project to the pool and its nodes, and the admins hear no agent_pool_empty for them.
A project with a ready node in its agent's pool stays there; a project of another member, a project with a repo and
every other run never go to the shared pool, and a node of the shared pool claims none of them.
a7: GET, PUT and DELETE /v1/admin/kg-pool and ``hub admin kg-pool`` are new; nothing that was there changes.
"""

from __future__ import annotations

from functools import partial

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, select, update

from evo_agents.hub import runs, tables
from evo_agents.hub.server import kg_init
from tests.hub.live import sql
from tests.hub.test_curator import OWNER, PROJECT, night  # noqa: F401 (night is a fixture)
from tests.hub.test_kg_init import init_row, profile, starter, with_profile
from tests.hub.test_kg_optimize import configured, project_id
from tests.hub.test_review_runs import REVIEW_KINDS, review  # noqa: F401 (a fixture)
from tests.hub.test_runs import OTHER, add_worker, claim, dispatched, web_client  # noqa: F401 (fixtures)

KG_POOL = "/v1/admin/kg-pool"
SHARED = "kg-shared"
INIT = f"/v1/kg/{PROJECT}/init"
EMPTY = "agent_pool_empty"


def user_id(db, login: str) -> int:
    ((found,),) = sql(db, select(tables.users.c.id).where(tables.users.c.login == login))
    return found


FEATURES = tuple(runs.WORKER_FEATURES)  # a daemon of today: it clones what it lacks and reviews with no repo
OLD = tuple(feature for feature in FEATURES if feature != runs.REPOLESS_REVIEW)


def kg_beat(client, worker: dict, features=FEATURES) -> None:
    body = {"runtimes": worker["runtimes"], "checkouts": {}, "free_slots": 1, "runs": [], "agent_version": "0.7.0",
            "run_kinds": REVIEW_KINDS, "features": list(features)}  # fmt: skip
    assert client.post("/v1/worker/heartbeat", json=body, headers=worker["headers"]).status_code == 200


def shared_node(world, features=FEATURES) -> dict:
    """The owner's pool kg-shared, named the shared KG pool by a hub admin, with one ready node that serves no project
    yet; its id is ``pool_id``."""
    client, db, headers = world.client, world.db, world.headers
    made = client.post("/v1/worker-pools", json={"name": SHARED, "projects": [PROJECT]}, headers=headers["owner"])
    assert made.status_code == 201, made.text
    worker = add_worker(client, headers["owner"], "kg-box", checkouts={}, pool=None)
    w, pp, wp = tables.workers, tables.worker_pool_projects, tables.worker_projects
    sql(db, update(w).values(pool_id=made.json()["id"]).where(w.c.id == worker["id"]))
    assert client.post("/v1/worker/doctor", json={"findings": []}, headers=worker["headers"]).status_code == 200
    sql(db, pp.delete().where(pp.c.pool_id == made.json()["id"]))  # a project the pool never served
    sql(db, wp.delete().where(wp.c.worker_id == worker["id"]))
    kg_beat(client, worker, features)
    named = client.put(KG_POOL, json={"owner": OWNER, "pool": SHARED}, headers=headers["admin"])
    assert named.status_code == 200, named.text
    return {**worker, "pool_id": made.json()["id"]}


def uploads_only(world, *, creator: str = OWNER) -> None:
    """The project fed by uploads alone: configured, built with a profile, no repo, created by ``creator``, and no
    ready node in its default agent's pool (night-mac left it)."""
    db = world.db
    pid = project_id(db)
    configured(world)
    with_profile(db, pid, profile())
    sql(db, tables.project_repos.delete().where(tables.project_repos.c.project_id == pid))
    sql(db, update(tables.projects).values(created_by=user_id(db, creator)).where(tables.projects.c.id == pid))
    sql(db, update(tables.workers).values(pool_id=None).where(tables.workers.c.id == world.worker["id"]))


def advanced(world) -> dict:
    engine = world.client.app.state.engine
    return world.client.portal.call(partial(kg_init.advance, engine, measure=starter))


def asked(world) -> dict:
    response = world.client.post(INIT, headers=world.headers["owner"])
    assert response.status_code == 201, response.text
    advanced(world)
    return init_row(world.db, response.json()["id"])


def run_row(db, run_id: int):
    r = tables.runs
    ((row),) = sql(db, select(r.c.kind, r.c.repos, r.c.selector, r.c.state).where(r.c.id == run_id))
    return row


def empty_notices(db) -> int:
    n = tables.notifications
    return sql(db, select(func.count()).select_from(n).where(n.c.notice_kind == EMPTY))[0][0]


def served(db, worker_id: int) -> list[int]:
    wp = tables.worker_projects
    return [row[0] for row in sql(db, select(wp.c.project_id).where(wp.c.worker_id == worker_id))]


# The KG runs of a project with no repo


def test_a_kg_run_of_a_project_with_no_repo_goes_to_the_shared_pool_which_serves_it_and_claims_it(review):
    client, db = review.client, review.db
    node = shared_node(review)
    uploads_only(review)
    row = asked(review)
    assert row["state"] == "running" and row["agent_run_id"], row["body"].get("notes")
    kind, repos, selector, state = run_row(db, row["agent_run_id"])
    assert (kind, repos, selector, state) == ("review", None, [f"pool={SHARED}"], "queued")
    assert empty_notices(db) == 0, "the run has a pool that takes it: no admin hears the agent's pool is empty"
    assert served(db, node["id"]) == [project_id(db)], "the hub adds the project to the shared pool's nodes"
    shown = client.get(KG_POOL, headers=review.headers["admin"]).json()
    assert shown == {"pool": SHARED, "owner": OWNER, "projects": [PROJECT]}
    spec = claim(client, node)
    assert spec is not None and spec["id"] == row["agent_run_id"]


def test_a_project_whose_agent_pool_has_a_ready_node_keeps_its_kg_runs_there(review):
    db = review.db
    shared_node(review)
    uploads_only(review)
    pools = tables.worker_pools
    home = select(pools.c.id).where(pools.c.name == f"agent-{PROJECT}").scalar_subquery()
    sql(db, update(tables.workers).values(pool_id=home).where(tables.workers.c.id == review.worker["id"]))
    kg_beat(review.client, review.worker)
    row = asked(review)
    assert row["state"] == "running", row["body"].get("notes")
    assert run_row(db, row["agent_run_id"])[2] == [f"pool=agent-{PROJECT}"]


def test_a_project_of_another_member_never_goes_to_the_shared_pool(review):
    client, db = review.client, review.db
    node = shared_node(review)
    uploads_only(review, creator=OTHER)
    row = asked(review)
    assert row["state"] == "queued" and row["agent_run_id"] is None
    assert "no review run" in row["body"]["notes"][-1]
    assert empty_notices(db) >= 1, "with no pool to take it, the admins hear the agent's pool is empty"
    assert project_id(db) not in served(db, node["id"])
    assert claim(client, node) is None


def test_a_project_with_a_repo_never_goes_to_the_shared_pool(review):
    db = review.db
    node = shared_node(review)
    uploads_only(review)
    pid = project_id(db)
    sql(db, tables.project_repos.insert().values(project_id=pid, name="evo-agents"))
    row = asked(review)
    assert row["agent_run_id"] is None and "no review run" in row["body"]["notes"][-1]
    assert pid not in served(db, node["id"]) and claim(review.client, node) is None


def test_a_shared_node_that_runs_no_review_with_no_repo_takes_none(review):
    node = shared_node(review, features=OLD)
    uploads_only(review)
    row = asked(review)
    assert row["agent_run_id"] is None and "runs no review with no repo" in row["body"]["notes"][-1]
    assert empty_notices(review.db) >= 1, "no node takes the run: the admins hear the agent's pool is empty"
    kg_beat(review.client, node)
    advanced(review)
    assert init_row(review.db)["agent_run_id"] is not None


def test_a_node_of_the_shared_pool_claims_no_other_run_even_one_that_asks_its_pool(review):
    """Placement by agent still holds for everything that is not a KG run: a plan run of the same project, sent to
    the shared pool by its selector, stays queued for the shared node; and a KG run of a project whose creator no
    longer owns the pool is not claimed either."""
    client, db = review.client, review.db
    node = shared_node(review)
    (other,) = dispatched(client, review.headers["owner"], [4])
    uploads_only(review)
    r = tables.runs
    sql(db, update(r).values(selector=[f"pool={SHARED}"], tolerations=[]).where(r.c.id == other["id"]))
    assert claim(client, node) is None, "a plan run of an agent goes to its agent's pool alone"
    sql(db, update(r).values(selector=[f"pool=agent-{PROJECT}"]).where(r.c.id == other["id"]))
    row = asked(review)
    sql(db, update(tables.projects).values(created_by=user_id(db, OTHER)).where(tables.projects.c.name == PROJECT))
    assert claim(client, node) is None, "the pool's owner no longer created the project"
    sql(db, update(tables.projects).values(created_by=user_id(db, OWNER)).where(tables.projects.c.name == PROJECT))
    assert claim(client, node)["id"] == row["agent_run_id"]


# The setting


def test_only_a_hub_admin_names_a_members_pool_and_clears_it(review):
    client, headers = review.client, review.headers
    shared_node(review)
    assert client.get(KG_POOL, headers=headers["owner"]).status_code == 403
    assert client.put(KG_POOL, json={"owner": OWNER, "pool": SHARED}, headers=headers["owner"]).status_code == 403
    missing = client.put(KG_POOL, json={"owner": OWNER, "pool": "nope"}, headers=headers["admin"])
    assert missing.status_code == 404
    agent = client.put(KG_POOL, json={"owner": OWNER, "pool": f"agent-{PROJECT}"}, headers=headers["admin"])
    assert agent.status_code == 422 and "agent's" in agent.json()["message"]
    again = client.put(KG_POOL, json={"owner": OWNER.upper(), "pool": SHARED.upper()}, headers=headers["admin"])
    assert again.status_code == 200 and again.json()["pool"] == SHARED
    cleared = client.delete(KG_POOL, headers=headers["admin"])
    assert cleared.status_code == 200 and cleared.json() == {"pool": None, "owner": None, "projects": []}
    a = tables.audit
    actions = sql(review.db, select(func.count()).select_from(a).where(a.c.action == "worker_pool.kg_shared"))
    assert actions[0][0] == 2, "named once, cleared once; naming it again changes nothing"
