"""Triage and the person when a work item of the Loop does not get on (``evo_agents.hub.server.loop.stall``,
``rules.failure_move``, ``rules.triaged_move``, the answer of a decision of category loop), as criterion a7 of plan loop-engine
names it, on the hub with Postgres.

A failure queues the work item again; the second opens its triage (the thread its failed run opened, or a thread of the
project's triage chat with the default agent asked) and the next hand-out waits for that thread's chat run, an hour at
most; the third asks the person its last run stands for, in a decision of category loop whose answer the hub does. A
work item waiting for the review of its SRS version, or for the settle chat of an open finding of level decide, waits
for a person and fails nothing. The chain is ``tests.hub.test_loop_tick``'s; the runs end as their reports would leave
them.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from functools import partial

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, select, update

from evo_agents.hub import tables
from evo_agents.hub.server.loop.model import ItemView, RunView, VersionView
from evo_agents.hub.server.loop.rules import RULES
from evo_agents.hub.server.loop.tick import tick
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import sql
from tests.hub.test_author_runs import CHECKOUTS as AUTHOR_CHECKOUTS
from tests.hub.test_author_runs import KINDS, publish_skill
from tests.hub.test_author_runs import beat as heartbeat
from tests.hub.test_ba_agent import members  # noqa: F401 (a fixture)
from tests.hub.test_chat_runs import chat_worker
from tests.hub.test_loop_admission import enable_strategist, queued
from tests.hub.test_loop_rules import NOW, view
from tests.hub.test_loop_tick import (  # noqa: F401 (client, hub and loop_core are fixtures)
    client,
    harness_listed,
    hub,
    item,
    ledger,
    loop_core,
    policy,
    project_id,
)
from tests.hub.test_runs import PROJECT, add_worker, audit_rows, claim, moved, report

app_key = credentials_api.app_key  # a fixture
BRAINSTORM = "brainstorm:G-1:r1"
BUILD = "build:srs:demo-shop:1:UC ORD-1"


def ticked(client, db) -> dict:
    return client.portal.call(partial(tick, client.app.state.engine, project_id(db)))


def ended(db, run_id: int, worker: dict, state: str = "failed") -> None:
    """Run ``run_id`` ended ``state`` on ``worker``, as its reports would leave it."""
    r = tables.runs
    values = {"state": state, "worker_id": worker["id"], "runtime": "claude-code", "leased_at": func.now()}
    values |= {"finished_at": func.now(), **({"error": "the agent stopped"} if state == "failed" else {})}
    sql(db, update(r).where(r.c.id == run_id).values(values))


def kinds_of(db, key: str) -> list[str]:
    return [kind for found, kind, _, _ in ledger(db) if found == key]


def answer(client, headers, decision_id: int, option: str):
    path = f"/v1/projects/{PROJECT}/decisions/{decision_id}/answer"
    return client.post(path, json={"option": option}, headers=headers)


def fail_twice(client, db, worker: dict) -> dict:
    """The brainstorm's run fails, it is handed out again, and that run fails: its triage opens."""
    ended(db, item(db, BRAINSTORM)["run_id"], worker)
    assert ticked(client, db)["dispatched"] == 1  # the first failure: queued again, and handed out at once
    ended(db, item(db, BRAINSTORM)["run_id"], worker)
    assert ticked(client, db)["triaged"] == 1
    return item(db, BRAINSTORM)


def test_loop_stall_a_task_run_failing_is_tried_again_then_triaged_then_its_person_decides(client, hub, hub_db):
    enable_strategist(client, hub)
    studio = chat_worker(client, hub["owner"], "studio", slots=3)  # the owner's: the triage thread's agent runs there
    policy(client, hub, {"enabled": True})
    assert ticked(client, hub_db)["dispatched"] == 1
    first = item(hub_db, BRAINSTORM)["run_id"]

    # failed twice: the work item watches its triage, a thread of the project's triage chat asking the default agent
    triaged = fail_twice(client, hub_db, studio)
    assert (triaged["state"], triaged["reason"], triaged["failures"], triaged["attempts"]) == (
        "watching",
        "triage",
        2,
        2,
    )
    triage = triaged["detail"]["triage"]
    assert triage["failures"] == 2 and triage["thread"].startswith("/chats/") and triage["run_id"] is not None
    m = tables.chat_messages
    ((body,),) = sql(hub_db, select(m.c.body).where(m.c.thread_root_id.is_(None)).order_by(m.c.id.desc()).limit(1))
    assert body.startswith(f"Work item #{triaged['id']} (brainstorm, {BRAINSTORM}) of the Loop of project {PROJECT}")
    assert f"- run #{first}: the run ended failed" in body and body.rstrip().split("\n")[-1].startswith("@")
    assert kinds_of(hub_db, BRAINSTORM) == ["created", "dispatched", "failed", "dispatched", "failed", "triaged"]

    # the third hand-out waits for the thread's chat run, which ends
    assert "dispatched" not in ticked(client, hub_db)
    ended(hub_db, triage["run_id"], studio, "done")
    assert ticked(client, hub_db)["dispatched"] == 1
    assert item(hub_db, BRAINSTORM)["state"] == "running"

    # the third failure asks owner, the run_as of @strategist, in a decision of category loop on the last run
    last = item(hub_db, BRAINSTORM)["run_id"]
    ended(hub_db, last, studio)
    assert ticked(client, hub_db)["asked"] == 1
    asked = item(hub_db, BRAINSTORM)
    assert (asked["state"], asked["reason"], asked["failures"]) == ("waiting_human", "loop_decision", 3)
    d, n = tables.decisions, tables.notifications
    ((decision_id, run_id, category, options),) = sql(hub_db, select(d.c.id, d.c.run_id, d.c.category, d.c.options))
    assert (run_id, category, [option["key"] for option in options]) == (last, "loop", ["retry", "drop", "manual"])
    assert asked["detail"]["decision"] == {"id": decision_id, "failures": 3, "attempts": 3, "run_id": last}
    assert sql(hub_db, select(n.c.kind).where(n.c.decision_id == decision_id)) == [("decision",)]
    assert "dispatched" not in ticked(client, hub_db), "it waits for the answer"

    # only owner answers; the hub does it: retry queues it again with its failures from 0
    assert answer(client, hub["chi"], decision_id, "retry").status_code == 403
    answered = answer(client, hub["owner"], decision_id, "retry")
    assert answered.status_code == 200 and answered.json()["state"] == "answered", answered.text
    retried = item(hub_db, BRAINSTORM)
    assert (retried["state"], retried["failures"]) == ("queued", 0)
    assert [row for row in audit_rows(hub_db, "decision")][-1][:2] == (
        "decision.answer",
        f"{PROJECT}/loop decision:{decision_id} run:{last} option=retry",
    )
    assert ticked(client, hub_db)["dispatched"] == 1
    assert answer(client, hub["owner"], decision_id, "drop").status_code == 409, "answered already"

    # three failures more and the answer manual: the work item cancelled, its chain left to a person
    triage = fail_twice(client, hub_db, studio)["detail"]["triage"]
    ended(hub_db, triage["run_id"], studio, "done")
    ticked(client, hub_db)
    ended(hub_db, item(hub_db, BRAINSTORM)["run_id"], studio)
    assert ticked(client, hub_db)["asked"] == 1
    second = item(hub_db, BRAINSTORM)["detail"]["decision"]["id"]
    assert answer(client, hub["owner"], second, "manual").status_code == 200
    final = item(hub_db, BRAINSTORM)
    assert (final["state"], final["reason"], final["finished_at"] is not None) == ("cancelled", "manual", True)
    assert kinds_of(hub_db, BRAINSTORM)[-1] == "cancelled"


def test_loop_stall_an_author_run_that_failed_twice_tells_its_own_triage_thread_and_waits_for_its_agent(
    client, hub, hub_db, tmp_path
):
    publish_skill(client, hub, tmp_path, "The author mode.")
    harness_listed(client, hub)
    studio = add_worker(client, hub["owner"], "studio", slots=3, checkouts=AUTHOR_CHECKOUTS)
    heartbeat(client, studio, KINDS)
    policy(client, hub, {"enabled": True, "worker": "studio"})
    queued(hub_db, "build", BUILD, use_case="UC ORD-1", srs_version=1)
    runs_failed = []
    for _ in range(2):
        ticked(client, hub_db)
        run_id = item(hub_db, BUILD)["run_id"]
        spec = claim(client, studio)
        assert spec is not None and spec["id"] == run_id, spec
        moved(client, studio, run_id, "running")
        assert report(client, studio, run_id, "failed", error="the agent stopped").status_code == 200
        t = tables.run_triage
        ((agent_run,),) = sql(hub_db, select(t.c.agent_run_id).where(t.c.run_id == run_id))
        runs_failed.append((run_id, agent_run))
        if len(runs_failed) == 1:
            ended(hub_db, agent_run, studio, "done")  # the first thread's agent is done; the next claim is the run's
    assert ticked(client, hub_db)["triaged"] == 1
    (_, _), (second, second_agent) = runs_failed
    triaged = item(hub_db, BUILD)
    assert (triaged["state"], triaged["reason"], triaged["detail"]["triage"]["run_id"]) == (
        "watching",
        "triage",
        second_agent,
    )
    t, m = tables.run_triage, tables.chat_messages
    ((root,),) = sql(hub_db, select(t.c.root_message_id).where(t.c.run_id == second))
    told = sql(hub_db, select(m.c.body).where(m.c.thread_root_id == root))
    assert [body for (body,) in told if body.startswith(f"Work item #{triaged['id']} (build")], told
    assert "dispatched" not in ticked(client, hub_db)
    ended(hub_db, second_agent, studio, "done")
    assert ticked(client, hub_db)["dispatched"] == 1


def test_loop_stall_waiting_for_a_review_or_a_settle_chat_is_a_persons_turn_and_no_failure():
    case = ItemView(30, "use_case", "use_case:northwind-pantry:CL-1:b4", "running", None, 1, run_id=40, runs=(40,))
    run = {40: RunView(40, "task", "done")}
    version = VersionView(70, 5, "demo-shop", 3, "in_review")
    review = RULES.reconcile(case, view(runs=run, drafts={30: version}))
    assert (review.state, review.reason, review.failed) == ("waiting_human", "srs_review", False)
    assert review.detail == {"srs": {"document": "demo-shop", "version": 3}}
    settle = replace(version, decide=(11, 12), chat="/chats/8?message=90")
    finding = RULES.reconcile(case, view(runs=run, drafts={30: settle}))
    assert (finding.state, finding.reason, finding.failed) == ("waiting_human", "srs_finding", False)
    assert finding.detail["srs"] == {"document": "demo-shop", "version": 3, "findings": [11, 12], "chat": settle.chat}
    waiting = replace(case, state="waiting_human", reason="srs_finding", detail=finding.detail)
    assert RULES.reconcile(waiting, view(runs=run, drafts={30: settle})) is None, "written once"
    changes = RULES.reconcile(waiting, view(runs=run, drafts={30: replace(version, state="draft", changes=True)}))
    assert (changes.state, changes.event, changes.failed) == ("queued", "ready", False)
    assert changes.detail == {"srs_changes": {"document": "demo-shop", "version": 3, "version_id": 70}}
    again = replace(case, detail=changes.detail)
    same = RULES.reconcile(again, view(runs=run, drafts={30: replace(version, state="draft", changes=True)}))
    assert (same.state, same.failed) == ("queued", True), "a run that answered the changes with no new version fails"
    withdrawn = RULES.reconcile(case, view(runs=run, drafts={30: replace(version, state="withdrawn")}))
    assert (withdrawn.state, withdrawn.reason) == ("cancelled", "person")
    nothing = RULES.reconcile(case, view(runs=run))
    assert (nothing.state, nothing.failed) == ("queued", True)


def test_loop_stall_counts_failures_to_triage_then_to_a_person_and_waits_an_hour_at_most():
    build = ItemView(50, "build", "build:srs:demo-shop:1:UC ORD-1", "running", None, 1, run_id=60, runs=(60,))
    lost = {60: RunView(60, "author", "failed")}
    steps = [RULES.reconcile(replace(build, failures=count), view(runs=lost)) for count in (0, 1, 2, 5)]
    assert [(move.state, move.reason, move.detail["failures"]) for move in steps] == [
        ("queued", None, 1),
        ("watching", "triage", 2),
        ("waiting_human", "loop_decision", 3),
        ("waiting_human", "loop_decision", 6),
    ]
    watching = replace(build, state="watching", reason="triage", run_id=None, failures=2, attempts=2)
    assert RULES.reconcile(watching, view()) is None, "the tick has not opened its thread"
    since = (NOW - timedelta(minutes=10)).isoformat()
    opened = replace(watching, detail={"triage": {"failures": 2, "attempts": 2, "at": since, "run_id": 70}})
    assert RULES.reconcile(opened, view(runs={70: RunView(70, "chat", "running")})) is None
    assert RULES.reconcile(opened, view(runs={70: RunView(70, "chat", "done")})).state == "queued"
    late = replace(
        opened, detail={"triage": {"failures": 2, "attempts": 2, "at": (NOW - timedelta(minutes=61)).isoformat()}}
    )
    assert RULES.reconcile(late, view(runs={70: RunView(70, "chat", "running")})).state == "queued"
    asking = replace(build, state="waiting_human", reason="loop_decision", run_id=None, failures=3)
    assert RULES.reconcile(asking, view()) is None, "the person's answer moves it"
