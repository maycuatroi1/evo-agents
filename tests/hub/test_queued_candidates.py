"""Why a queued run waits (``evo_agents.hub.server.runs.service.queue_fit``): GET .../rerun-candidates of a queued run
lists each worker that could take it with why it can or cannot, as its claim counts it, and the agent pools of the
project; when no worker can, its diagnosis says why with the command that fixes it, and so does the warning of the
dispatch that queued it. Each case checks the claim agrees: a worker said to be no fit claims nothing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import delete, update

from evo_agents.hub import tables
from evo_agents.hub.server import profile_store
from tests.hub.live import sql
from tests.hub.test_agent_pool_placement import ADMIN_GRANT, OWNER, join, make_agent
from tests.hub.test_agent_pool_placement import worker as pool_worker
from tests.hub.test_runs import (  # noqa: F401  (fixtures)
    PLAN,
    PROJECT,
    add_worker,
    claim,
    client,
    dispatched,
    hub,
)

SERVE = f"`evo-agents worker projects --worker laptop --add {PROJECT}`"


def candidates(client, headers, run_id: int) -> dict:
    response = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}/rerun-candidates", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def by_name(answer: dict) -> dict:
    return {worker["worker"]: worker for worker in answer["workers"]}


def serves_nothing(db, worker: dict) -> None:
    sql(db, delete(tables.worker_projects).where(tables.worker_projects.c.worker_id == worker["id"]))


def test_a_run_no_worker_serves_the_project_of_says_so_with_the_command(client, hub, hub_db):
    laptop = add_worker(client, hub["owner"], "laptop")
    serves_nothing(hub_db, laptop)
    (run,) = dispatched(client, hub["owner"], [2])
    assert run["state"] == "queued"
    assert run["warning"] == f"run #{run['id']} is queued, but no worker serves project {PROJECT}: {SERVE}"
    answer = candidates(client, hub["owner"], run["id"])
    assert (answer["state"], answer["failure_cause"]) == ("queued", None)
    assert answer["diagnosis"] == f"no worker serves project {PROJECT}: {SERVE}"
    found = by_name(answer)["laptop"]
    assert not found["fit"] and found["rank"] is None
    assert found["reasons"] == [f"does not serve project {PROJECT}: {SERVE}"]
    assert claim(client, laptop) is None


def test_the_command_names_the_nearest_worker_with_the_most_free_slots(client, hub, hub_db):
    small = add_worker(client, hub["owner"], "small", slots=1)
    roomy = add_worker(client, hub["owner"], "roomy", slots=4)
    for worker in (small, roomy):
        serves_nothing(hub_db, worker)
    (run,) = dispatched(client, hub["owner"], [2])
    assert run["warning"].endswith(f"`evo-agents worker projects --worker roomy --add {PROJECT}`")
    assert [worker["worker"] for worker in candidates(client, hub["owner"], run["id"])["workers"]] == ["roomy", "small"]


def test_a_plan_run_no_worker_can_take_comes_with_a_warning(client, hub, hub_db):
    laptop = add_worker(client, hub["owner"], "laptop")
    serves_nothing(hub_db, laptop)
    response = client.post(f"/v1/projects/{PROJECT}/plan-runs", json={"plan_id": PLAN}, headers=hub["owner"])
    assert response.status_code == 201, response.text
    assert response.json()["warning"].endswith(f"no worker serves project {PROJECT}: {SERVE}")


def test_a_run_whose_only_worker_is_offline_names_it(client, hub, hub_db):
    laptop = add_worker(client, hub["owner"], "laptop")
    gone = datetime.now(UTC) - timedelta(hours=2)
    w = tables.workers
    sql(hub_db, update(w).where(w.c.id == laptop["id"]).values(last_heartbeat_at=gone))
    (run,) = dispatched(client, hub["owner"], [2])
    assert "the nearest, laptop, is offline: its last heartbeat was 1" in run["warning"]
    answer = candidates(client, hub["owner"], run["id"])
    found = by_name(answer)["laptop"]
    assert not found["fit"] and found["reasons"][0].startswith("is offline")
    assert answer["diagnosis"].startswith("no worker can take it; the nearest, laptop, is offline")


def test_a_run_of_a_runtime_no_worker_has_names_the_runtime(client, hub):
    laptop = add_worker(client, hub["owner"], "laptop")
    (run,) = dispatched(client, hub["owner"], [2], runtime="codex")
    reason = "does not report codex available, which the run asks for"
    assert run["warning"] == f"run #{run['id']} is queued, but no worker can take it; the nearest, laptop, {reason}"
    assert by_name(candidates(client, hub["owner"], run["id"]))["laptop"]["reasons"] == [reason]
    assert claim(client, laptop) is None


def test_a_run_a_worker_can_take_comes_with_no_warning_and_the_worker_claims_it(client, hub):
    laptop = add_worker(client, hub["owner"], "laptop")
    busy = add_worker(client, hub["owner"], "busy")
    (held,) = dispatched(client, hub["owner"], [4], worker_id=busy["id"])
    assert claim(client, busy)["id"] == held["id"]
    (run,) = dispatched(client, hub["owner"], [2])
    assert run["warning"] is None
    answer = candidates(client, hub["owner"], run["id"])
    assert answer["diagnosis"] is None
    workers = by_name(answer)
    assert (workers["laptop"]["fit"], workers["laptop"]["rank"], workers["laptop"]["reasons"]) == (
        True,
        1,
        ["can take it now"],
    )
    assert workers["busy"]["fit"] and workers["busy"]["rank"] == 2  # it waits for a slot, and takes it then
    assert workers["busy"]["reasons"] == ["has no free slot (1 of 1 held): it takes the run once one frees"]
    assert claim(client, laptop)["id"] == run["id"]


def test_the_candidates_of_a_queued_run_are_its_owners(client, hub):
    add_worker(client, hub["owner"], "laptop")
    (run,) = dispatched(client, hub["owner"], [2])
    path = f"/v1/projects/{PROJECT}/runs/{run['id']}/rerun-candidates"
    assert client.get(path, headers=hub["other"]).status_code == 403
    assert client.get(path, headers=hub["stranger"]).status_code == 404


def test_a_run_of_an_agent_whose_pool_is_empty_says_how_to_add_a_node(client, hub, hub_db, monkeypatch):
    monkeypatch.setattr(profile_store, "NEW_AGENT_PLACEMENT", "pool")
    grant = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=hub["admin"])
    assert grant.status_code == 200, grant.text
    make_agent(client, hub["owner"], "helper", db=hub_db)
    mine = pool_worker(client, hub["owner"], "mine")  # the owner's worker, outside the pool
    (run,) = dispatched(client, hub["owner"], [2], profile="helper")
    fix = "`evo-agents worker pool token create agent-helper`"
    assert (
        f"agent @helper runs in pool mode, and its pool agent-helper has no node: create a join token with {fix}"
        in (run["warning"])
    )
    answer = candidates(client, hub["owner"], run["id"])
    assert answer["workers"] == []  # only the nodes of its pool take it
    (pool,) = [item for item in answer["pools"] if item["pool"] == "agent-helper"]
    assert (pool["takes_run"], pool["nodes"], pool["ready_nodes"], pool["fit"]) == (True, 0, 0, False)
    assert pool["reasons"][0].startswith("has no node: `evo-agents worker pool token create agent-helper`")
    assert claim(client, mine) is None
    node = join(client, hub_db, pool_worker(client, hub["other"], "node"), "helper")
    answer = candidates(client, hub["owner"], run["id"])
    assert answer["diagnosis"] is None and by_name(answer)["node"]["fit"]
    (pool,) = [item for item in answer["pools"] if item["pool"] == "agent-helper"]
    assert (pool["nodes"], pool["ready_nodes"], pool["fit"]) == (1, 1, True)
    assert claim(client, node)["id"] == run["id"]
