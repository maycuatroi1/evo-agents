"""Who can run a failed run again (``evo_agents.hub.server.runs.service.candidates``, GET .../runs/{id}/rerun-candidates),
as criterion a3 of the plan run-failure-triage names it: each worker of the run's owner with whether it is fit and why,
ranked by what the run failed of, and each agent profile of the project by fit; the owner's alone, for a run that
failed."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_chat_runs import beat as chat_beat
from tests.hub.test_runs import (  # noqa: F401  (fixtures)
    CHECKOUTS,
    PROJECT,
    add_worker,
    claim,
    client,
    dispatched,
    hub,
    moved,
)

OTHER_REPO = {f"{PROJECT}/m1-kb-docs": {"path": "/src/m1-kb-docs", "branch": "main"}}


def candidates(client, headers, run_id: int):
    return client.get(f"/v1/projects/{PROJECT}/runs/{run_id}/rerun-candidates", headers=headers)


def failed_on(client, hub, worker: dict, cause: str | None, step: int = 2) -> int:
    """A run of ``step`` that ``worker`` claimed and that failed with ``cause``."""
    (run,) = dispatched(client, hub["owner"], [step], worker_id=worker["id"])
    spec = claim(client, worker)
    assert spec["id"] == run["id"]
    moved(client, worker, run["id"], "running", "failed", error="it stopped", failure_cause=cause)
    return run["id"]


def by_name(answer: dict) -> dict:
    return {worker["worker"]: worker for worker in answer["workers"]}


def test_candidates_list_each_worker_with_why_it_fits_or_not(client, hub):
    old = add_worker(client, hub["owner"], "old", slots=2)
    add_worker(client, hub["owner"], "spare", slots=2)
    add_worker(client, hub["owner"], "elsewhere", slots=2, checkouts=OTHER_REPO)
    run_id = failed_on(client, hub, old, "verify_failed")
    response = candidates(client, hub["owner"], run_id)
    assert response.status_code == 200, response.text
    answer = response.json()
    assert_json_keys("hub run candidates", answer)
    assert (answer["run_id"], answer["state"], answer["failure_cause"]) == (run_id, "failed", "verify_failed")
    workers = by_name(answer)
    assert [workers[name]["rank"] for name in ("old", "spare")] == [1, 2]  # equal fit: the smaller id first
    assert workers["old"]["is_run_worker"] and workers["old"]["fit"] and workers["old"]["runtimes"] == ["claude-code"]
    assert not workers["elsewhere"]["fit"] and workers["elsewhere"]["rank"] is None
    assert workers["elsewhere"]["reasons"] == ["has no checkout of evo-agents"]
    assert answer["accounts"] == []


def test_candidates_put_the_worker_of_the_run_first_for_a_missing_program_and_last_for_a_stopped_worker(client, hub):
    old = add_worker(client, hub["owner"], "old", slots=3)
    add_worker(client, hub["owner"], "spare", slots=3)
    missing = failed_on(client, hub, old, "missing_tool", step=2)
    stopped = failed_on(client, hub, old, "worker_stopped", step=4)
    ranks = {
        run_id: {name: w["rank"] for name, w in by_name(candidates(client, hub["owner"], run_id).json()).items()}
        for run_id in (missing, stopped)
    }
    assert ranks[missing] == {"old": 1, "spare": 2}
    assert ranks[stopped] == {"old": 2, "spare": 1}


def test_candidates_say_why_a_worker_cannot_take_the_run(client, hub):
    old = add_worker(client, hub["owner"], "old", slots=1)
    busy = add_worker(client, hub["owner"], "busy", slots=1)
    (held,) = dispatched(client, hub["owner"], [4], worker_id=busy["id"])  # queued for it: its one slot is taken
    run_id = failed_on(client, hub, old, None)
    workers = by_name(candidates(client, hub["owner"], run_id).json())
    assert held["state"] == "queued"
    assert workers["busy"]["reasons"] == ["has no free slot"] and workers["busy"]["free_slots"] == 0
    assert workers["old"]["fit"]


def test_candidates_list_the_agents_of_the_project_by_fit(client, hub):
    old = add_worker(client, hub["owner"], "old")
    run_id = failed_on(client, hub, old, "missing_tool")
    agent = {"handle": "builder", "name": "Builder", "permissions": {"tool": {"mode": "allowlist", "allow": ["read"]}}}
    grant = {"role": "admin", "max_level": "internal"}
    assert client.put(f"/v1/admin/projects/{PROJECT}/grants/owner", json=grant, headers=hub["admin"]).status_code == 200
    created = client.post(f"/v1/projects/{PROJECT}/agents", json=agent, headers=hub["owner"])
    assert created.status_code == 201, created.text
    agents = candidates(client, hub["owner"], run_id).json()["agents"]
    assert "takes no chat run" in agents[0]["reasons"][0]  # the daemon of 0.4.0 runs no chat run: no agent is fit
    chat_beat(client, old)  # its daemon says it runs chat runs and applies profiles
    agents = candidates(client, hub["owner"], run_id).json()["agents"]
    by_handle = {item["handle"]: item for item in agents}
    assert by_handle["builder"]["fit"] is False
    assert "does not allow bash" in " ".join(by_handle["builder"]["reasons"])
    assert agents[0]["fit"] and agents[0]["rank"] == 1 and agents[-1]["handle"] == "builder"


def test_candidates_are_the_owners_for_a_run_that_failed(client, hub):
    old = add_worker(client, hub["owner"], "old")
    run_id = failed_on(client, hub, old, None)
    assert candidates(client, hub["other"], run_id).status_code == 403
    assert candidates(client, hub["stranger"], run_id).status_code == 404
    (queued,) = dispatched(client, hub["owner"], [4])
    refusal = candidates(client, hub["owner"], queued["id"])
    assert refusal.status_code == 409 and "only a failed run has candidates" in refusal.json()["message"]
    assert CHECKOUTS  # the checkouts the workers above report
