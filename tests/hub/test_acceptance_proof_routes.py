"""One line of proof for each acceptance criterion, through the hub's routes (step 4 of the super-board-parity-2
plan), with Postgres.

A step run's claim names the criteria of its step, and the proofs of its state report land on the plan's step when
the run is done. A plan run's step report with proofs is refused (422) when one is missing, naming it, and keeps
them on the step when there is one for each; a report without proofs, from an older worker, is taken as before."""

import pytest

from tests.hub import test_plan_runs as plan_runs
from tests.hub import test_runs as runs
from tests.hub.test_plan_runs import client, fleet_step, push, started, step_report  # noqa: F401
from tests.hub.test_runs import PASSED, SHA, add_worker, claim, control, dispatched, members, moved, report, step

A4 = "Each criterion of the step has a line of proof."
A9 = "The contract golden is regenerated."
ACCEPTANCE = [{"id": "a4", "criterion": A4}, {"id": "a9", "criterion": A9, "invariant": True}]
ROLLOUT = runs.plan_body
PROOFS = [
    {"id": "a4", "kind": "test", "locator": "tests/hub/test_x.py::test_y"},
    {"id": "a9", "kind": "n/a", "reason": "no contract here"},
]


def with_acceptance(body: dict, key: int) -> dict:
    body["acceptance"] = ACCEPTANCE
    next(item for item in body["steps"] if item["id"] == key)["acceptance"] = ["a4", "a9"]
    return body


@pytest.fixture
def hub(client, github, monkeypatch) -> dict:  # noqa: F811
    """test_runs' members with step 2 of rollout and of fleet naming a4 and a9."""
    monkeypatch.setattr(runs, "plan_body", lambda: with_acceptance(ROLLOUT(), 2))
    headers = members(client, github)
    push(client, headers["owner"], with_acceptance(plan_runs.plan_body(), 2))
    return headers


def test_acceptance_proof_a_step_run_names_its_criteria_and_its_proofs_reach_the_step(client, hub):
    worker = add_worker(client, hub["owner"], "mac-mini")
    run_id = dispatched(client, hub["owner"], [2])[0]["id"]
    held = claim(client, worker)
    assert held["id"] == run_id and held["criteria"] == ["a4", "a9"]
    assert f"- a4: {A4}" in held["prompt"] and '"proofs"' in held["prompt"]
    moved(client, worker, run_id, "running", "verifying")
    bad = report(client, worker, run_id, "review", verify=PASSED, proofs=[{"id": "a4", "kind": "n/a"}])
    assert bad.status_code == 422, bad.text
    review = moved(client, worker, run_id, "review", verify=PASSED, commit_sha=SHA, proofs=PROOFS)
    assert review["state"] == "review" and step(client, hub["owner"], 2).get("proofs") is None
    assert control(client, hub["owner"], run_id, "approve").status_code == 200  # kept on the run until done
    finished = step(client, hub["owner"], 2)
    assert finished["status"] == "done" and finished["proofs"] == PROOFS


def test_acceptance_proof_a_step_run_of_a_step_without_acceptance_names_none(client, hub):
    worker = add_worker(client, hub["owner"], "mac-mini")
    dispatched(client, hub["owner"], [4])
    held = claim(client, worker)
    assert "criteria" not in held and "Acceptance criteria" not in held["prompt"]


def test_acceptance_proof_a_plan_run_step_report_missing_a_line_gets_422_naming_it(client, hub):
    worker, run = started(client, hub)
    refused = step_report(
        client, worker, run["id"], 2, "done", verify=PASSED, commit_sha=SHA, evidence="done", proofs=PROOFS[:1]
    )
    assert refused.status_code == 422, refused.text
    message = refused.json()["message"]
    assert "step 2 is not done" in message and f"a9: {A9}" in message and "a4:" not in message
    assert fleet_step(client, hub["owner"], 2)["status"] != "done"
    early = step_report(client, worker, run["id"], 2, "in_progress", proofs=PROOFS)
    assert early.status_code == 422, early.text
    taken = step_report(
        client, worker, run["id"], 2, "done", verify=PASSED, commit_sha=SHA, evidence="done", proofs=PROOFS
    )
    assert taken.status_code == 200, taken.text
    kept = fleet_step(client, hub["owner"], 2)
    assert kept["status"] == "done" and kept["proofs"] == PROOFS and kept["evidence"].endswith("\ndone")


def test_acceptance_proof_a_report_without_proofs_from_an_older_worker_is_taken(client, hub):
    worker, run = started(client, hub)
    taken = step_report(client, worker, run["id"], 2, "done", verify=PASSED, commit_sha=SHA, evidence="done")
    assert taken.status_code == 200, taken.text
    assert "proofs" not in fleet_step(client, hub["owner"], 2)
