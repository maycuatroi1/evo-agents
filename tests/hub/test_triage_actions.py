"""What the agent of a triage thread does on its own (``evo_agents.hub.server.triage_actions``), as criteria a5 and a7 of
the plan run-failure-triage name them: it reruns the failed step and edits the plan of the failed run inside the frame
the hub enforces, each act leaves a ``Decision:`` line in the thread and an audit row, and what is outside the frame is
refused: a step that is done, a clause of the verify dropped, a chain of more than two reruns, a spent budget."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select, update

from evo_agents.hub import tables
from tests.hub.live import sql
from tests.hub.test_chat_runs import chat_worker, thread
from tests.hub.test_runs import PLAN, PROJECT, claim, client, dispatched, hub, moved, plan, report  # noqa: F401
from tests.hub.test_triage import failed_step, row_of

OWNER_LOGIN = "owner"


def agent_of(client, worker, run_id: int, hub_db) -> int:
    """Claim and start the chat run of the triage of ``run_id``; its id."""
    ((_, _, _, _, agent_run),) = row_of(hub_db, run_id)
    for _ in range(4):
        spec = claim(client, worker)
        if spec and spec["id"] == agent_run:
            moved(client, worker, agent_run, "running")
            return agent_run
    raise AssertionError(f"chat run {agent_run} was not claimed")


def rerun(client, holder, agent_run: int, **body):
    body = {"reason": "the program is installed now", **body}
    return client.post(f"/v1/worker/runs/{agent_run}/triage/rerun", json=body, headers=holder["headers"])


def edit(client, hub, worker, agent_run: int, step: int, **body):
    held = plan(client, hub["owner"])["revision"]
    body = {"step": str(step), "reason": "the check was too narrow", "if_revision": held, **body}
    return client.post(f"/v1/worker/runs/{agent_run}/triage/plan", json=body, headers=worker["headers"])


def said(client, hub, hub_db, run_id: int) -> list[str]:
    ((chat_id, root_id, *_),) = row_of(hub_db, run_id)
    return [m["body"] for m in thread(client, hub["owner"], chat_id, root_id)]


def audited(hub_db, action: str) -> list[str]:
    return [row[0] for row in sql(hub_db, select(tables.audit.c.target).where(tables.audit.c.action == action))]


def setup(client, hub, hub_db, cause="missing_tool", key=2, machine="mac-mini", **kw):
    worker = chat_worker(client, hub["owner"], machine, slots=4)
    run_id = failed_step(client, hub, worker, key, cause=cause, **kw)
    return worker, run_id, agent_of(client, worker, run_id, hub_db)


def test_triage_rerun_runs_the_failed_step_again_on_its_worker_with_a_decision_line_and_an_audit_row(
    client, hub, hub_db
):
    worker, run_id, agent = setup(client, hub, hub_db)
    done = rerun(client, worker, agent)
    assert done.status_code == 201, done.text
    new_id = done.json()["run_id"]
    r = tables.runs
    assert sql(hub_db, select(r.c.parent_run_id, r.c.state, r.c.pinned_worker_id).where(r.c.id == new_id)) == [
        (run_id, "queued", worker["id"])
    ]
    lines = said(client, hub, hub_db, run_id)
    assert lines[-1].startswith("Decision: ran run #") and f"run #{new_id}" in lines[-1]
    assert "the program is installed now" in lines[-1]
    (target,) = audited(hub_db, "run.triage_rerun")
    assert f"run:{run_id}" in target and "agent:@" in target and f"triage_run:{agent}" in target
    t = tables.run_triage
    assert sql(hub_db, select(t.c.auto_reruns).where(t.c.run_id == run_id)) == [(1,)]
    again = rerun(client, worker, agent)
    assert again.status_code in (409, 422), again.text


def test_triage_rerun_of_a_cause_with_no_policy_or_a_guess_the_agent_did_not_confirm_is_refused(client, hub, hub_db):
    worker, run_id, agent = setup(client, hub, hub_db, cause="credentials")
    refused = rerun(client, worker, agent)
    assert refused.status_code == 422 and "no automatic rerun" in refused.json()["message"]
    assert audited(hub_db, "run.triage_rerun") == []
    worker2, run2, agent2 = setup(
        client, hub, hub_db, cause=None, key=4, machine="mac-book", error="connection reset by peer"
    )
    t = tables.run_triage
    guessed = sql(hub_db, select(t.c.cause, t.c.cause_inferred).where(t.c.run_id == run2))[0]
    if guessed[1] and guessed[0] in ("network", "hub_5xx", "missing_tool"):
        refused = rerun(client, worker2, agent2)
        assert refused.status_code == 422 and "--confirm-cause" in refused.json()["message"]
        assert rerun(client, worker2, agent2, cause_confirmed=True).status_code == 201


def test_triage_rerun_is_for_the_agent_of_a_chat_run_only(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4)
    step_run = dispatched(client, hub["owner"], [2])[0]["id"]
    assert claim(client, worker)["id"] == step_run
    assert rerun(client, worker, step_run).status_code == 404


def test_triage_plan_edit_of_a_done_step_is_refused_and_the_plan_stays_as_it_was(client, hub, hub_db):
    worker, run_id, agent = setup(client, hub, hub_db, cause="verify_failed")
    before = plan(client, hub["owner"])
    refused = edit(client, hub, worker, agent, 1, verify="true")
    assert refused.status_code == 422 and "only a pending step may be edited" in refused.json()["message"]
    refused = edit(client, hub, worker, agent, 1, what="m")
    assert refused.status_code == 422
    after = plan(client, hub["owner"])
    assert (after["revision"], after["body"]) == (before["revision"], before["body"])
    assert audited(hub_db, "run.triage_plan_edit") == []
    assert not any(line.startswith("Decision:") for line in said(client, hub, hub_db, run_id))


def test_triage_plan_edit_that_drops_a_verify_clause_or_widens_the_what_or_names_another_step_is_refused(
    client, hub, hub_db
):
    worker, run_id, agent = setup(client, hub, hub_db, cause="verify_failed")
    assert edit(client, hub, worker, agent, 2, verify="test -f").status_code == 201  # same count: one command
    # step 2's verify now has one command, and an empty one has none
    refused = edit(client, hub, worker, agent, 2, verify="")
    assert refused.status_code == 422 and "dropping a check" in refused.json()["message"]
    refused = edit(client, hub, worker, agent, 2, what="the queue, and much more besides")
    assert refused.status_code == 422 and "only narrowed" in refused.json()["message"]
    assert edit(client, hub, worker, agent, 99, verify="x").status_code == 422
    assert edit(client, hub, worker, agent, 2, verify="x", if_revision=1).status_code == 409  # a stale revision


def test_triage_plan_edit_with_a_rerun_writes_a_revision_as_the_owner_and_queues_the_step_again(client, hub, hub_db):
    worker, run_id, agent = setup(client, hub, hub_db, cause="verify_failed")
    start = plan(client, hub["owner"])["revision"]
    alone = rerun(client, worker, agent)
    assert alone.status_code == 422 and "only together with an edit" in alone.json()["message"]
    done = edit(
        client, hub, worker, agent, 2, verify="test -f feature.txt && test -s feature.txt", what="queue", rerun=True
    )
    assert done.status_code == 201, done.text
    answer = done.json()
    assert answer["plan_revision"] == start + 1 and answer["run_id"] is not None
    held = plan(client, hub["owner"])
    step = next(item for item in held["body"]["steps"] if item["id"] == 2)
    assert (held["revision"], held["updated_by"], step["verify"], step["what"]) == (
        start + 1,
        OWNER_LOGIN,
        "test -f feature.txt && test -s feature.txt",
        "queue",
    )
    assert next(item for item in held["body"]["steps"] if item["id"] == 1)["status"] == "done"
    line = said(client, hub, hub_db, run_id)[-1]
    assert line.startswith("Decision: changed verify and what of step 2") and "old verify: test -f feature.txt" in line
    (target,) = audited(hub_db, "run.triage_plan_edit")
    assert f"run:{run_id}" in target and f"revision:{start + 1}" in target and f"rerun:{answer['run_id']}" in target


def test_triage_plan_edit_whose_rerun_is_refused_leaves_the_plan_as_it_was(client, hub, hub_db):
    worker, run_id, agent = setup(client, hub, hub_db, cause="verify_failed")
    t = tables.run_triage
    start = plan(client, hub["owner"])["revision"]
    sql(hub_db, update(t).values(auto_reruns=1).where(t.c.run_id == run_id))
    refused = edit(client, hub, worker, agent, 2, verify="test -f feature.txt && true", rerun=True)
    assert refused.status_code == 409, refused.text
    held = plan(client, hub["owner"])
    assert held["revision"] == start
    assert next(item for item in held["body"]["steps"] if item["id"] == 2)["verify"] == "test -f feature.txt"


def test_triage_rerun_stops_at_two_reruns_in_a_chain_and_at_one_for_each_cause(client, hub, hub_db):
    worker, run_id, agent = setup(client, hub, hub_db, cause="network")
    first = rerun(client, worker, agent).json()["run_id"]
    spec = claim(client, worker)
    assert spec["id"] == first
    assert report(client, worker, first, "failed", error="reset", failure_cause="network").status_code == 200
    agent2 = agent_of(client, worker, first, hub_db)
    same_cause = rerun(client, worker, agent2)
    assert same_cause.status_code == 409 and "for cause network" in same_cause.json()["message"]
    sql(hub_db, update(tables.runs).values(failure_cause="hub_5xx").where(tables.runs.c.id == first))
    second = rerun(client, worker, agent2)
    assert second.status_code == 201, second.text
    third_id = second.json()["run_id"]
    assert claim(client, worker)["id"] == third_id
    assert report(client, worker, third_id, "failed", error="502", failure_cause="runtime").status_code == 200
    ((_, _, _, _, agent3),) = row_of(hub_db, third_id)
    assert agent3 is None  # the chain is full: the thread says so and asks no agent
    assert any("2 times by itself already, the most" in line for line in said(client, hub, hub_db, third_id))
    assert len(audited(hub_db, "run.triage_rerun")) == 2


def test_triage_rerun_over_the_chat_budget_is_refused(client, hub, hub_db):
    worker, run_id, agent = setup(client, hub, hub_db)
    sql(hub_db, update(tables.chats).values(budget_usd=0.01))
    sql(hub_db, update(tables.runs).values(usage={"total_cost_usd": 1.0}).where(tables.runs.c.kind == "chat"))
    refused = rerun(client, worker, agent)
    assert refused.status_code == 409 and "budget" in refused.json()["message"]
    assert audited(hub_db, "run.triage_rerun") == []


def test_triage_rerun_on_a_worker_the_owner_does_not_have_is_refused(client, hub, hub_db):
    worker, run_id, agent = setup(client, hub, hub_db, cause="runtime")
    refused = rerun(client, worker, agent, **{"worker": "no-such-machine"})
    assert refused.status_code == 422 and "no worker no-such-machine" in refused.json()["message"]
