"""Where a run goes, step 2 of the worker-pools plan (criteria a2 and a3, and a9 for what stays as it was):
``evo_agents.hub.server.worker_placement`` and the claim, the rerun candidates and the worker views that read it.

a2: a node of a pool is ready only while its newest doctor report is at most 48 hours old and has no high finding its
pool does not accept; a node that is not takes no run, and says why as ready and ready_reason in GET /v1/workers/{id}
and in its pool's nodes; a worker of no pool is never gated.
a3: a run's labels (and pool=<name>) must all be labels of the worker that claims it, its own, its pool's or the ones
the hub gives it (os=, arch=, runtime=<name>); a worker with taints, its own (PATCH) or its pool's, takes only runs
that tolerate each, or one pinned to it.
a9: a run with no selector, on a worker with no taint, is claimed as before.
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
)

POOLS = "/v1/worker-pools"
HIGH = {"code": "other-admin", "severity": "high", "detail": "carol administers this machine (group sudo)."}
MEDIUM = {"code": "machine-token-on-worker", "severity": "medium", "detail": "~/.evo/hub/token is a machine token."}


def pool(client, headers, name: str = "ci", **settings) -> dict:
    response = client.post(POOLS, json={"name": name, "projects": [PROJECT], **settings}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def node(client, headers, db, name: str, of_pool: dict) -> dict:
    """A worker of ``headers``' member, made a node of ``of_pool``."""
    worker = add_worker(client, headers, name)
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
    ci = pool(client, hub["owner"])
    lab = node(client, hub["owner"], hub_db, "lab", ci)
    (run,) = dispatched(client, hub["owner"], [2])
    view = shown(client, hub["owner"], lab)
    assert view["ready"] is False and "sent no doctor report" in view["ready_reason"]
    listed = pool_node(client, hub["owner"], "ci", lab)
    assert (listed["ready"], listed["ready_reason"]) == (False, view["ready_reason"])
    assert claim(client, lab) is None
    doctor(client, lab, MEDIUM)  # a medium finding does not gate it
    assert (shown(client, hub["owner"], lab)["ready"], shown(client, hub["owner"], lab)["ready_reason"]) == (True, None)
    assert pool_node(client, hub["owner"], "ci", lab)["ready"] is True
    assert claim(client, lab)["id"] == run["id"]


def test_a_pool_node_whose_newest_doctor_report_is_older_than_48_hours_is_not_ready(client, hub, hub_db):
    ci = pool(client, hub["owner"])
    lab = node(client, hub["owner"], hub_db, "lab", ci)
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
    pool(client, hub["owner"], name="strict")
    loose = pool(client, hub["owner"], name="loose", accepted_findings=["other-admin"])
    strict = node(client, hub["owner"], hub_db, "strict-1", {"id": _pool_id(client, hub["owner"], "strict")})
    easy = node(client, hub["owner"], hub_db, "loose-1", loose)
    doctor(client, strict, HIGH)
    doctor(client, easy, HIGH)
    view = shown(client, hub["owner"], strict)
    assert view["ready"] is False
    assert "other-admin (high), which pool strict does not accept" in view["ready_reason"]
    assert pool_node(client, hub["owner"], "strict", strict)["ready"] is False
    assert shown(client, hub["owner"], easy)["ready"] is True
    (pinned,) = dispatched(client, hub["owner"], [2], worker_id=strict["id"])
    (free,) = dispatched(client, hub["owner"], [4])
    assert claim(client, strict) is None  # a run pinned to it waits for it to be ready too
    assert claim(client, easy)["id"] == free["id"]
    assert run_view(client, hub["owner"], pinned["id"])["state"] == "queued"


def _pool_id(client, headers, name: str) -> int:
    response = client.get(f"{POOLS}/{name}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_a_worker_of_no_pool_is_always_ready_without_a_doctor_report(client, hub, hub_db):
    mac = add_worker(client, hub["owner"], "mac")
    view = shown(client, hub["owner"], mac)
    assert (view["ready"], view["ready_reason"], view["taints"]) == (True, None, [])
    assert {"os=darwin", "arch=arm64", "runtime=claude-code"} <= set(view["auto_labels"])
    (run,) = dispatched(client, hub["owner"], [2])
    assert claim(client, mac)["id"] == run["id"]


def test_a_run_with_no_selector_on_an_untainted_worker_is_claimed_as_before(client, hub, hub_db):
    mac = add_worker(client, hub["owner"], "mac")
    (run,) = dispatched(client, hub["owner"], [2])
    view = run_view(client, hub["owner"], run["id"])
    assert (view["selector"], view["tolerations"]) == ([], [])
    assert claim(client, mac)["id"] == run["id"]


def test_a_selector_run_goes_only_to_a_worker_with_every_label(client, hub, hub_db):
    plain = add_worker(client, hub["owner"], "plain")
    gpu = add_worker(client, hub["owner"], "gpu")
    w = tables.workers
    sql(hub_db, update(w).where(w.c.id == gpu["id"]).values(labels=["gpu"]))
    (run,) = dispatched(client, hub["owner"], [2], labels=["GPU", "os=darwin", "runtime=claude-code"])
    view = run_view(client, hub["owner"], run["id"])
    assert (view["selector"], view["tolerations"]) == (["gpu", "os=darwin", "runtime=claude-code"], [])
    assert claim(client, plain) is None
    assert claim(client, gpu)["id"] == run["id"]
    # an auto label it lacks keeps it from any worker
    (linux,) = dispatched(client, hub["owner"], [4], labels=["os=linux"])
    assert claim(client, plain) is None
    assert run_view(client, hub["owner"], linux["id"])["state"] == "queued"


def test_a_pool_run_goes_only_to_a_ready_node_of_that_pool(client, hub, hub_db):
    ci = pool(client, hub["owner"])
    lab = node(client, hub["owner"], hub_db, "lab", ci)
    mac = add_worker(client, hub["owner"], "mac")
    (run,) = dispatched(client, hub["owner"], [2], pool="ci")
    assert run_view(client, hub["owner"], run["id"])["selector"] == ["pool=ci"]
    assert claim(client, mac) is None
    assert claim(client, lab) is None  # not ready yet
    doctor(client, lab)
    assert claim(client, lab)["id"] == run["id"]


def test_a_tainted_worker_takes_only_runs_that_tolerate_its_taints_or_are_pinned_to_it(client, hub, hub_db):
    box = add_worker(client, hub["owner"], "box")
    answer = tainted(client, hub["owner"], box, "Dedicated", "dedicated")
    assert answer["taints"] == ["dedicated"]
    assert shown(client, hub["owner"], box)["taints"] == ["dedicated"]
    (run,) = dispatched(client, hub["owner"], [2])
    assert claim(client, box) is None
    other = add_worker(client, hub["owner"], "other")
    assert claim(client, other)["id"] == run["id"]
    (tolerating,) = dispatched(client, hub["owner"], [4], tolerations=["dedicated"])
    assert run_view(client, hub["owner"], tolerating["id"])["tolerations"] == ["dedicated"]
    assert claim(client, box)["id"] == tolerating["id"]


def test_a_run_pinned_to_a_tainted_worker_goes_there_whatever_its_taints(client, hub, hub_db):
    box = add_worker(client, hub["owner"], "box")
    tainted(client, hub["owner"], box, "dedicated")
    (run,) = dispatched(client, hub["owner"], [2], worker_id=box["id"])
    assert claim(client, box)["id"] == run["id"]


def test_a_pools_taints_apply_to_its_nodes(client, hub, hub_db):
    ci = pool(client, hub["owner"], taints=["ci-only"])
    lab = node(client, hub["owner"], hub_db, "lab", ci)
    doctor(client, lab)
    assert shown(client, hub["owner"], lab)["taints"] == ["ci-only"]
    (run,) = dispatched(client, hub["owner"], [2])
    assert claim(client, lab) is None
    (tolerating,) = dispatched(client, hub["owner"], [4], pool="ci", tolerations=["ci-only"])
    assert claim(client, lab)["id"] == tolerating["id"]
    assert run_view(client, hub["owner"], run["id"])["state"] == "queued"


def test_the_rerun_candidates_count_the_selector_and_taints_as_the_claim_does(client, hub, hub_db):
    old = add_worker(client, hub["owner"], "old")
    box = add_worker(client, hub["owner"], "box")
    tainted(client, hub["owner"], box, "dedicated")
    (run,) = dispatched(client, hub["owner"], [2], labels=["os=darwin"])
    assert claim(client, old)["id"] == run["id"]
    moved(client, old, run["id"], "running", "failed", error="it stopped", failure_cause="verify_failed")
    w = tables.workers
    sql(hub_db, update(w).where(w.c.id == old["id"]).values(os="linux"))
    response = client.get(f"/v1/projects/{PROJECT}/runs/{run['id']}/rerun-candidates", headers=hub["owner"])
    assert response.status_code == 200, response.text
    workers = {item["worker"]: item for item in response.json()["workers"]}
    assert not workers["old"]["fit"] and "lacks the label os=darwin the run asks" in workers["old"]["reasons"]
    assert not workers["box"]["fit"]
    assert any("has the taint dedicated" in reason for reason in workers["box"]["reasons"])


def test_placement_flags_are_checked(client, hub):
    add_worker(client, hub["owner"], "mac")
    for bad in ({"labels": ["no spaces"]}, {"tolerations": ["=x"]}, {"pool": "a b"}):
        response = dispatch(client, hub["owner"], [2], **bad)
        assert response.status_code == 422, (bad, response.text)
