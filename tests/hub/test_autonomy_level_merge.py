"""K5 and K6 at the merge queue and the deploy to PROD (``evo_agents.hub.server.autonomy_gates``), as criterion a6 of
plan autonomy-levels names them: the hub reads the mode of the principal of the run that made a merge request before it
merges it (K5) and before it deploys its merge to PROD (K6). deny leaves the act undone with a reason that names the
class, the mode and the source, and writes the audit row ``autonomy.refused``; ask opens a decision of category
``autonomy`` to the principal and acts once they approve; notify acts, then sends the notice ``autonomy``. A principal
at A5 merges as before (``tests.hub.test_merge_gate``). The world is ``tests.hub.test_merge_gate``'s; the projects and
people are fictional."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import runs, tables
from tests.hub.live import sql
from tests.hub.test_credentials_api import audit_rows
from tests.hub.test_merge_gate import (  # noqa: F401 (world: a fixture)
    PROJECT,
    REPO,
    gate_pass,
    green,
    judged,
    one,
    plan_done,
    world,
)
from tests.hub.test_post_merge import JOBS, ends, lines, on_dev, played, policy
from tests.hub.test_runs import OWNER

MERGE_KEY = f"merge {REPO}!1"


def pref(world, **classes) -> None:
    body = {"level": "A5", "classes": classes}
    response = world.client.put("/v1/autonomy/prefs", json=body, headers=world.headers["owner"])
    assert response.status_code == 200, response.text


def through_the_gates(world):
    """A plan's merge request, its run done, through every gate up to the Judge's verdict; (the run, the request)."""
    run_id = plan_done(world)
    assert gate_pass(world) == {"opened": 1}
    (merge_request,) = world.project.merge_requests.values()
    green(world, merge_request)
    assert gate_pass(world) == {"judging": 1}
    judged(world, head=merge_request.sha)
    return run_id, merge_request


def autonomy_decisions(world) -> list:
    d = tables.decisions
    query = select(d.c.id, d.c.run_id, d.c.step_key, d.c.state).where(d.c.category == runs.AUTONOMY_DECISION_CATEGORY)
    return sql(world.db, query.order_by(d.c.id))


def answer(world, decision_id: int, option: str) -> None:
    path = f"/v1/projects/{PROJECT}/decisions/{decision_id}/answer"
    response = world.client.post(path, json={"option": option}, headers=world.headers["owner"])
    assert response.status_code == 200, response.text


def autonomy_notices(world) -> list[dict]:
    response = world.client.get("/v1/me/notifications", headers=world.headers["owner"])
    assert response.status_code == 200, response.text
    return [item for item in response.json()["notifications"] if item["notice_kind"] == "autonomy"]


def test_autonomy_level_merge_under_deny_blocks_the_merge_request_with_the_class_mode_and_source(world):
    pref(world, K5="deny")
    run_id, merge_request = through_the_gates(world)
    entry = one(world)
    assert entry["state"] == "blocked" and merge_request.state != "merged"
    assert "K5 (merge into the default or target branch) is deny" in entry["reason"]
    assert f"set by pref:{OWNER}" in entry["reason"]
    assert gate_pass(world).get("merged") is None and merge_request.state != "merged"
    ((target, login, project, _),) = audit_rows(world.db, "autonomy.refused")
    assert target == f"run:{run_id} K5 {MERGE_KEY} mode=deny" and login == OWNER and project == PROJECT


def test_autonomy_level_merge_under_ask_waits_for_the_principal_then_merges_once_approved(world):
    pref(world, K5="ask")
    run_id, merge_request = through_the_gates(world)
    entry = one(world)
    assert entry["state"] == "judging" and merge_request.state != "merged"
    ((decision_id, decided_run, key, state),) = autonomy_decisions(world)
    assert (decided_run, key, state) == (run_id, MERGE_KEY, "open")
    assert f"decision #{decision_id}" in entry["reason"]
    gate_pass(world)  # still open: it waits, and asks no second time
    assert one(world)["state"] == "judging" and len(autonomy_decisions(world)) == 1
    ((target, *_),) = audit_rows(world.db, "autonomy.asked")
    assert target == f"run:{run_id} K5 {MERGE_KEY} decision:{decision_id}"

    answer(world, decision_id, "approve")
    gate_pass(world)
    assert one(world)["state"] in ("ready", "merged")
    if merge_request.state != "merged":
        gate_pass(world)
    assert merge_request.state == "merged" and one(world)["state"] == "merged"


def test_autonomy_level_merge_under_ask_a_refusal_leaves_it_unmerged(world):
    pref(world, K5="ask")
    through_the_gates(world)
    ((decision_id, *_),) = autonomy_decisions(world)
    answer(world, decision_id, "refuse")
    gate_pass(world)
    entry = one(world)
    assert entry["state"] == "blocked" and f"refused it in decision #{decision_id}" in entry["reason"]
    assert "K5 (merge into the default or target branch) is ask" in entry["reason"]


def test_autonomy_level_merge_under_notify_merges_then_tells_the_principal(world):
    pref(world, K5="notify")
    run_id, merge_request = through_the_gates(world)
    assert one(world)["state"] == "ready"
    assert gate_pass(world) == {"merged": 1} and merge_request.state == "merged"
    (notice,) = autonomy_notices(world)
    assert notice["title"].startswith(f"The hub merged !1 of {REPO}") and notice["run_id"] == run_id
    ((target, *_),) = audit_rows(world.db, "autonomy.notified")
    assert target == f"run:{run_id} K5 {MERGE_KEY}"
    assert audit_rows(world.db, "autonomy.refused") == []


def merged_by_a_run(world, **classes):
    """The policy deploys lumen-kb-docs to PROD at once; a plan's merge request of a run whose principal holds
    ``classes``, merged and its UAT check passed on dev; (the run, its pipeline on dev)."""
    policy(world, jobs=JOBS)
    pref(world, **classes)
    run_id, merge_request = through_the_gates(world)
    assert gate_pass(world) == {"merged": 1}
    pipeline = on_dev(world, merge_request)
    gate_pass(world)
    ends(world, pipeline, "uat-smoke", "success")
    gate_pass(world)
    return run_id, pipeline


def test_autonomy_level_deploy_under_deny_leaves_prod_undeployed_with_the_reason(world):
    run_id, _ = merged_by_a_run(world, K6="deny")
    assert "deploy-prod" not in played(world)
    entry = one(world)
    assert lines(entry, "prod_deploy") == []
    assert "K6 (deploy, publish, tag a release) is deny" in entry["reason"] and f"pref:{OWNER}" in entry["reason"]
    gate_pass(world)
    assert "deploy-prod" not in played(world)
    ((target, *_),) = audit_rows(world.db, "autonomy.refused")  # once, however many passes
    assert target == f"run:{run_id} K6 deploy {REPO}!1 mode=deny"


def test_autonomy_level_deploy_under_ask_deploys_once_the_principal_approves(world):
    run_id, _ = merged_by_a_run(world, K6="ask")
    assert "deploy-prod" not in played(world)
    ((decision_id, decided_run, key, _),) = autonomy_decisions(world)
    assert (decided_run, key) == (run_id, f"deploy {REPO}!1")
    assert f"decision #{decision_id}" in one(world)["reason"]
    answer(world, decision_id, "approve")
    gate_pass(world)
    assert played(world)[-1] == "deploy-prod"


def test_autonomy_level_deploy_under_notify_deploys_then_tells_the_principal(world):
    run_id, _ = merged_by_a_run(world, K6="notify")
    assert played(world)[-1] == "deploy-prod"
    (notice,) = autonomy_notices(world)
    assert notice["title"].startswith(f"The hub deployed !1 of {REPO} to PROD") and notice["run_id"] == run_id
