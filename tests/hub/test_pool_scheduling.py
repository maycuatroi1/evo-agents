"""Where a run goes, step 2 of the worker-pools plan (criteria a2 and a3, and a9 for what stays as it was), in the
pools of the agents (plan agent-dedicated-pools): ``evo_agents.hub.server.worker_placement`` and the claim, the rerun
candidates and the worker views that read it.

a2: a node of a pool is ready only while its newest doctor report is at most 48 hours old and has no high finding its
pool does not accept; a node that is not takes no run, and says why as ready and ready_reason in GET /v1/workers/{id}
and in its pool's nodes; a worker of no pool is never gated, and takes no run of an agent.
a3: a run of an agent asks pool=agent-<handle> and tolerates its pool's taints (``agent_placement.stamped``), and a
node of that pool takes it whatever its own labels and taints; a dispatch picks no labels or taints
(``tests.hub.test_agent_pool_placement``). A run of no agent (one from before agents) still goes by labels and taints:
its labels must all be labels of the worker that claims it, its own, its pool's or the ones the hub gives it (os=,
arch=, runtime=<name>); a worker with taints takes only such runs that tolerate each, or one pinned to it.
a9: a run of no agent with no selector, on a worker with no taint, is claimed as before.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, update

from evo_agents.hub import tables
from tests.hub.live import sql
from tests.hub.test_runs import (  # noqa: F401  (fixtures)
    PROJECT,
    add_worker,
    claim,
    client,
    dispatch,
    dispatched,
    hub,
    moved,
    set_run,
)

POOLS = "/v1/worker-pools"
HOME = f"agent-{PROJECT}"  # the pool of the project's default agent, where a run that names no agent goes
HIGH = {"code": "other-admin", "severity": "high", "detail": "carol administers this machine (group sudo)."}
MEDIUM = {"code": "machine-token-on-worker", "severity": "medium", "detail": "~/.evo/hub/token is a machine token."}


def pool(client, headers, name: str = "ci", **settings) -> dict:
    response = client.post(POOLS, json={"name": name, "projects": [PROJECT], **settings}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def home(client, hub) -> dict:
    """The pool of the project's default agent, as a hub admin reads it."""
    response = client.get(f"{POOLS}/{HOME}", headers=hub["admin"])
    assert response.status_code == 200, response.text
    return response.json()


def node(client, headers, db, name: str, of_pool: dict) -> dict:
    """A worker of ``headers``' member, made a node of ``of_pool`` with no doctor report yet."""
    worker = add_worker(client, headers, name, pool=None)
    sql(db, update(tables.workers).where(tables.workers.c.id == worker["id"]).values(pool_id=of_pool["id"]))
    return worker


def doctor(client, worker: dict, *findings: dict) -> dict:
    response = client.post("/v1/worker/doctor", json={"findings": list(findings)}, headers=worker["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def shown(client, headers, worker: dict) -> dict:
    response = client.get(f"/v1/workers/{worker['id']}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def pool_node(client, headers, name: str, worker: dict) -> dict:
    response = client.get(f"{POOLS}/{name}", headers=headers)
    assert response.status_code == 200, response.text
    (found,) = [item for item in response.json()["nodes"] if item["id"] == worker["id"]]
    return found


def tainted(client, headers, worker: dict, *taints: str) -> dict:
    response = client.patch(f"/v1/workers/{worker['id']}", json={"taints": list(taints)}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def run_view(client, headers, run_id: int) -> dict:
    response = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_a_pool_node_without_a_doctor_report_is_not_ready_and_claims_nothing(client, hub, hub_db):
    lab = node(client, hub["owner"], hub_db, "lab", home(client, hub))
    (run,) = dispatched(client, hub["owner"], [2])
    view = shown(client, hub["owner"], lab)
    assert view["ready"] is False and "sent no doctor report" in view["ready_reason"]
    listed = pool_node(client, hub["admin"], HOME, lab)
    assert (listed["ready"], listed["ready_reason"]) == (False, view["ready_reason"])
    assert claim(client, lab) is None
    doctor(client, lab, MEDIUM)  # a medium finding does not gate it
    assert (shown(client, hub["owner"], lab)["ready"], shown(client, hub["owner"], lab)["ready_reason"]) == (True, None)
    assert pool_node(client, hub["admin"], HOME, lab)["ready"] is True
    assert claim(client, lab)["id"] == run["id"]


def test_a_pool_node_whose_newest_doctor_report_is_older_than_48_hours_is_not_ready(client, hub, hub_db):
    lab = node(client, hub["owner"], hub_db, "lab", home(client, hub))
    doctor(client, lab)
    w = tables.workers
    stale = func.now() - timedelta(hours=49)
    sql(hub_db, update(w).where(w.c.id == lab["id"]).values(doctor_at=stale))
    dispatched(client, hub["owner"], [2])
    view = shown(client, hub["owner"], lab)
    assert view["ready"] is False and "older than 48 hours" in view["ready_reason"]
    assert claim(client, lab) is None
    sql(hub_db, update(w).where(w.c.id == lab["id"]).values(doctor_at=func.now() - timedelta(hours=47)))
    assert shown(client, hub["owner"], lab)["ready"] is True
    assert claim(client, lab) is not None


def test_a_pool_node_with_a_high_finding_is_ready_only_when_its_pool_accepts_the_code(client, hub, hub_db):
    strict = node(client, hub["owner"], hub_db, "strict-1", home(client, hub))
    doctor(client, strict, HIGH)
    view = shown(client, hub["owner"], strict)
    assert view["ready"] is False
    assert f"other-admin (high), which pool {HOME} does not accept" in view["ready_reason"]
    assert pool_node(client, hub["admin"], HOME, strict)["ready"] is False
    (pinned,) = dispatched(client, hub["owner"], [2])
    set_run(hub_db, pinned["id"], pinned_worker_id=strict["id"])
    assert claim(client, strict) is None  # a run pinned to it waits for it to be ready too
    assert run_view(client, hub["owner"], pinned["id"])["state"] == "queued"
    accepted = client.patch(f"{POOLS}/{HOME}", json={"accepted_findings": ["other-admin"]}, headers=hub["admin"])
    assert accepted.status_code == 200, accepted.text
    assert shown(client, hub["owner"], strict)["ready"] is True
    assert claim(client, strict)["id"] == pinned["id"]


def test_a_worker_of_no_pool_is_always_ready_without_a_doctor_report_and_takes_no_run_of_an_agent(client, hub, hub_db):
    mac = add_worker(client, hub["owner"], "mac", pool=None)
    view = shown(client, hub["owner"], mac)
    assert (view["ready"], view["ready_reason"], view["taints"]) == (True, None, [])
    assert {"os=darwin", "arch=arm64", "runtime=claude-code"} <= set(view["auto_labels"])
    (run,) = dispatched(client, hub["owner"], [2])
    assert claim(client, mac) is None, "a run of an agent goes to a node of the agent's pool alone"


def agentless(db, run_id: int, **placement) -> None:
    """Run ``run_id`` made a run of no agent, as one dispatched before agents, placed by ``placement`` (its selector,
    tolerations or pin; none by default)."""
    set_run(db, run_id, profile_id=None, profile_version=None, **{"selector": [], "tolerations": [], **placement})


def test_a_run_with_no_selector_on_an_untainted_worker_is_claimed_as_before(client, hub, hub_db):
    mac = add_worker(client, hub["owner"], "mac", pool=None)
    (run,) = dispatched(client, hub["owner"], [2])
    view = run_view(client, hub["owner"], run["id"])
    assert (view["selector"], view["tolerations"]) == ([f"pool={HOME}"], []), "the hub put the run of an agent there"
    agentless(hub_db, run["id"])
    assert claim(client, mac)["id"] == run["id"]


def test_a_selector_run_goes_only_to_a_worker_with_every_label(client, hub, hub_db):
    plain = add_worker(client, hub["owner"], "plain", pool=None)
    gpu = add_worker(client, hub["owner"], "gpu", pool=None)
    w = tables.workers
    sql(hub_db, update(w).where(w.c.id == gpu["id"]).values(labels=["gpu"]))
    run, linux = dispatched(client, hub["owner"], [2, 4])
    agentless(hub_db, run["id"], selector=["gpu", "os=darwin", "runtime=claude-code"])
    agentless(hub_db, linux["id"], selector=["os=linux"])
    assert claim(client, plain) is None
    assert claim(client, gpu)["id"] == run["id"]
    # an auto label it lacks keeps it from any worker
    assert claim(client, plain) is None
    assert run_view(client, hub["owner"], linux["id"])["state"] == "queued"


def test_a_tainted_worker_takes_only_runs_that_tolerate_its_taints_or_are_pinned_to_it(client, hub, hub_db):
    """A run of no agent; a node of an agent's pool takes the runs of its agent whatever its own taints (the claim's
    placement gate passes a run of the node's own agent: ``claim_rules._gates``)."""
    box = add_worker(client, hub["owner"], "box", slots=2, pool=None)
    answer = tainted(client, hub["owner"], box, "Dedicated", "dedicated")
    assert answer["taints"] == ["dedicated"]
    assert shown(client, hub["owner"], box)["taints"] == ["dedicated"]
    loose, tolerating = dispatched(client, hub["owner"], [4, 5])
    agentless(hub_db, loose["id"])
    agentless(hub_db, tolerating["id"], tolerations=["dedicated"])
    assert run_view(client, hub["owner"], tolerating["id"])["tolerations"] == ["dedicated"]
    assert claim(client, box)["id"] == tolerating["id"]
    assert claim(client, box) is None, "a free slot, but the other run does not tolerate its taint"
    set_run(hub_db, loose["id"], pinned_worker_id=box["id"])
    assert claim(client, box)["id"] == loose["id"], "pinned to it, a run goes there whatever its taints"

    node = add_worker(client, hub["owner"], "node")  # a ready node of the pool of the project's default agent
    tainted(client, hub["owner"], node, "dedicated")
    (theirs,) = dispatched(client, hub["owner"], [2])
    assert claim(client, node)["id"] == theirs["id"], "its own taints keep no run of its agent from it"


def test_a_pools_taints_apply_to_its_nodes(client, hub, hub_db):
    patched = client.patch(f"{POOLS}/{HOME}", json={"taints": ["ci-only"]}, headers=hub["admin"])
    assert patched.status_code == 200, patched.text
    lab = add_worker(client, hub["owner"], "lab")  # a ready node of the pool
    assert shown(client, hub["owner"], lab)["taints"] == ["ci-only"]
    (run,) = dispatched(client, hub["owner"], [2])
    assert run_view(client, hub["owner"], run["id"])["tolerations"] == ["ci-only"], "a run of its agent tolerates them"
    assert claim(client, lab)["id"] == run["id"]


def test_the_rerun_candidates_count_the_selector_and_taints_as_the_claim_does(client, hub, hub_db):
    old = add_worker(client, hub["owner"], "old")  # a ready node of the pool of the project's default agent
    box = add_worker(client, hub["owner"], "box", pool=None)
    tainted(client, hub["owner"], box, "dedicated")
    (run,) = dispatched(client, hub["owner"], [2])
    assert claim(client, box) is None
    assert claim(client, old)["id"] == run["id"]
    moved(client, old, run["id"], "running", "failed", error="it stopped", failure_cause="verify_failed")
    response = client.get(f"/v1/projects/{PROJECT}/runs/{run['id']}/rerun-candidates", headers=hub["owner"])
    assert response.status_code == 200, response.text
    workers = {item["worker"]: item for item in response.json()["workers"]}
    assert workers["old"]["fit"], workers["old"]["reasons"]
    assert not workers["box"]["fit"] and f"lacks the label pool={HOME} the run asks" in workers["box"]["reasons"]
    assert any("has the taint dedicated" in reason for reason in workers["box"]["reasons"])


def test_placement_flags_are_checked(client, hub):
    add_worker(client, hub["owner"], "mac")
    for bad in ({"labels": ["no spaces"]}, {"tolerations": ["=x"]}, {"pool": "a b"}):
        response = dispatch(client, hub["owner"], [2], **bad)
        assert response.status_code == 422, (bad, response.text)
