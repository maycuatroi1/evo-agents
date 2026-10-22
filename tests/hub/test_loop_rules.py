"""The engine rules/1 of the Loop (``evo_agents.hub.server.loop.rules``), as criterion a4 of plan loop-engine and the
table of rules of the report's section Loop name it: what each delta makes, its idempotent key, and where each work
item moves, stale ones cancelled. The engine is pure: these tests hand it events and views and read what it returns,
with no Postgres. The canvas, Northwind Pantry, and the SRS document demo-shop are made up.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("sqlalchemy")

from evo_agents.hub.autonomy import effective
from evo_agents.hub.server.loop import model
from evo_agents.hub.server.loop.model import (
    ApprovalView,
    ClaimView,
    Event,
    GoalView,
    ItemView,
    MergeView,
    Note,
    RunView,
    TaskView,
    Transition,
    View,
    WorkSpec,
)
from evo_agents.hub.server.loop.rules import RULES
from evo_agents.hub.tables_srs import frame_task_key

NOW = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
POLICY = effective({"loop": {"enabled": True}}, {})["loop"]
GOAL = GoalView(1, "G-1", 3, "active", True, False, 10, 1, 7)
CLAIM = ClaimView(21, 10, "northwind-pantry", "CL-1", "testing", ("UC ORD-1",))
FRAME = frame_task_key("demo-shop", 2, "UC ORD-1")


def view(**changes) -> View:
    base = View(now=NOW, policy=POLICY, goals={GOAL.id: GOAL}, claims={CLAIM.id: CLAIM})
    return replace(base, **changes)


def baseline(*transitions: dict, number: int = 4, at: datetime = NOW, event_id: int = 50) -> Event:
    payload = {"v": 1, "canvas": "northwind-pantry", "canvas_id": 10, "baseline_id": 90 + number}
    payload |= {"baseline": number, "kind": "push", "transitions": list(transitions)}
    return Event(event_id, "canvas.baseline", payload, at)


def moved(to: str, claim_id: int = 21, code: str = "CL-1", before: str = "assumption") -> dict:
    return {"claim_id": claim_id, "claim": code, "from": before, "to": to, "evidence": []}


def approved(added=("UC ORD-1",), changed=(), version: int = 2, event_id: int = 60) -> Event:
    items = {"added": {"use_case": list(added)} if added else {}, "changed": {"use_case": list(changed)}}
    payload = {"v": 1, "document": "demo-shop", "document_id": 5, "version": version, "version_id": 40 + version}
    payload |= {"previous": version - 1, "approver": "someone-else", "approver_id": 8, "items": items}
    return Event(event_id, "srs.version.approved", payload, NOW)


def use_case(state: str = "queued", **changes) -> ItemView:
    item = ItemView(100, "use_case", "use_case:northwind-pantry:CL-1:b4", state, None, GOAL.id, claim_id=CLAIM.id)
    return replace(item, **changes)


def build(state: str = "watching", reason: str | None = "auto_tasks", **changes) -> ItemView:
    detail = {"frame": FRAME, "version_id": 42}
    item = ItemView(200, "build", f"build:{FRAME}", state, reason, GOAL.id, claim_id=CLAIM.id, detail=detail)
    return replace(item, **changes)


# What an event makes


def test_loop_rules_a_claim_moved_to_testing_makes_one_use_case_keyed_by_its_baseline():
    (spec,) = RULES.rules(baseline(moved("testing")), view())
    assert spec == WorkSpec(
        "use_case",
        "use_case:northwind-pantry:CL-1:b4",
        GOAL.id,
        claim_id=21,
        owner="@ba",
        detail={"baseline": 4, "from": "assumption"},
    )
    assert model.CYCLE_OF[spec.kind] == "business"
    # the same baseline told twice gives the same key: the tick makes it once
    assert RULES.rules(baseline(moved("testing"), event_id=51), view()) == [spec]
    # a claim of a canvas no active goal with a metric drives makes nothing
    assert RULES.rules(baseline(moved("testing")), view(goals={1: replace(GOAL, canvas_id=11)})) == []
    assert RULES.rules(baseline(moved("testing")), view(goals={1: replace(GOAL, has_metric=False)})) == []
    assert RULES.rules(baseline(moved("testing")), view(goals={1: replace(GOAL, status="paused")})) == []
    # the project's one active goal with a metric and no canvas takes every canvas
    (spec,) = RULES.rules(baseline(moved("testing")), view(goals={1: replace(GOAL, canvas_id=None)}))
    assert spec.goal_id == GOAL.id


def test_loop_rules_a_use_case_waits_out_the_stable_hours_of_the_policy():
    stable = view(policy={**POLICY, "stable_hours": 2})
    (spec,) = RULES.rules(baseline(moved("testing"), at=NOW - timedelta(hours=1)), stable)
    assert (spec.state, spec.reason, spec.detail["stable_at"]) == (
        "watching",
        "stable",
        (NOW + timedelta(hours=1)).isoformat(),
    )
    waiting = use_case("watching", reason="stable", detail=spec.detail)
    assert RULES.reconcile(waiting, stable) is None
    later = replace(stable, now=NOW + timedelta(hours=1))
    assert RULES.reconcile(waiting, later) == Transition(100, "queued", "ready")
    (spec,) = RULES.rules(baseline(moved("testing"), at=NOW - timedelta(hours=3)), stable)
    assert (spec.state, spec.reason) == ("queued", None)


def test_loop_rules_validated_and_invalidated_are_noted_in_the_chain_and_make_nothing():
    assert RULES.rules(baseline(moved("validated", before="testing")), view()) == []  # no chain yet: nothing to note
    chain = view(items=(use_case("done"), build("queued", None, task_id=300)), tasks={300: TaskView(300, 1, "todo")})
    (note,) = RULES.rules(baseline(moved("validated", before="testing")), chain)
    assert note == Note(
        21,
        "noted:northwind-pantry:CL-1:b4",
        {"status": "validated", "canvas": "northwind-pantry", "claim": "CL-1", "baseline": 4},
    )
    (ended,) = RULES.rules(baseline(moved("invalidated", before="testing")), chain)
    assert (ended.comment_on, ended.author_id) == ((300,), GOAL.owner_id)
    assert "CL-1 of canvas northwind-pantry is invalidated since baseline 4" in ended.comment
    closed = replace(chain, tasks={300: TaskView(300, 1, "done")})
    (retired,) = RULES.rules(baseline(moved("retired", before="testing")), closed)
    assert (retired.comment, retired.comment_on) == (None, ())  # no open frame task to tell


def test_loop_rules_an_approved_version_of_a_use_case_closes_it_and_makes_a_build_per_new_use_case():
    case = use_case("running", run_id=500, runs=(499, 500))
    found = RULES.rules(
        approved(added=("UC ORD-1", "UC ORD-2")), view(items=(case,), versions={42: 499}, tasks_on=True)
    )
    done, first, second = found
    assert done == Transition(100, "done", "done", detail={"document": "demo-shop", "version": 2})
    assert (first.key, first.state, first.reason, first.parent_id) == (f"build:{FRAME}", "watching", "auto_tasks", 100)
    assert (first.claim_id, first.srs_document_id, first.use_case, first.srs_version) == (21, 5, "UC ORD-1", 2)
    assert first.detail == {"frame": FRAME, "version_id": 42}
    assert second.key == "build:" + frame_task_key("demo-shop", 2, "UC ORD-2")
    assert model.CYCLE_OF[first.kind] == "development"


def test_loop_rules_a_version_adding_a_use_case_a_claim_in_a_chain_links_answers_that_chain_alone():
    linked = view(items=(use_case(),), tasks_on=True)
    done, spec = RULES.rules(approved(added=("UC ORD-1", "UC RET-1")), linked)
    assert done.item_id == 100 and spec.use_case == "UC ORD-1"  # the claim links UC ORD-1 alone
    # a version that changes the use case closes the work item and makes no build: auto_tasks makes no frame
    assert RULES.rules(approved(added=(), changed=("uc  ord-1",)), linked) == [
        Transition(100, "done", "done", detail={"document": "demo-shop", "version": 2})
    ]
    # outside every chain, or with the plugin tasks off, nothing is built
    assert RULES.rules(approved(added=("UC SHIP-1",)), linked) == []
    assert RULES.rules(approved(), replace(linked, tasks_on=False)) == [
        Transition(100, "done", "done", detail={"document": "demo-shop", "version": 2})
    ]
    assert RULES.rules(approved(), view(tasks_on=True)) == []  # a claim with no chain


def test_loop_rules_other_events_make_nothing():
    for kind in ("tasks.status", "canvas.claim", "signal.posted"):
        assert RULES.rules(Event(70, kind, {"v": 1}, NOW), view(items=(use_case(), build()))) == []


# What the clock makes


def test_loop_rules_a_goal_gets_a_brainstorm_for_each_revision_then_each_week_one_at_a_time():
    (spec,) = RULES.scheduled(view())
    assert spec == WorkSpec("brainstorm", "brainstorm:G-1:r3", 1, owner="@strategist", detail={"revision": 3})
    keys = frozenset({"brainstorm:G-1:r3"})
    assert RULES.scheduled(view(keys=keys)) == []  # its week has not come
    week_at = NOW - timedelta(hours=1)
    weekly = view(keys=keys, week="2026-W43", week_at=week_at, brainstormed={1: NOW - timedelta(days=3)})
    (spec,) = RULES.scheduled(weekly)
    assert (spec.key, spec.detail) == ("brainstorm:G-1:2026-W43", {"week": "2026-W43"})
    assert RULES.scheduled(replace(weekly, keys=keys | {spec.key})) == []
    assert RULES.scheduled(replace(weekly, proposals={1: 2})) == []  # proposals of the last one still open
    assert RULES.scheduled(replace(weekly, brainstormed={1: NOW})) == []  # one was made since the week's time
    running = ItemView(9, "brainstorm", "brainstorm:G-1:r2", "running", None, 1)
    assert RULES.scheduled(view(items=(running,))) == []  # one at a time
    assert RULES.scheduled(view(goals={1: replace(GOAL, reached=True)})) == []
    assert RULES.scheduled(view(goals={1: replace(GOAL, reached=None)})) != []  # no signal yet: not met
    for goal in (replace(GOAL, has_metric=False), replace(GOAL, status="closed")):
        assert RULES.scheduled(view(goals={1: goal})) == []


# Where a work item moves


def test_loop_rules_stale_work_items_are_cancelled_by_goal_claim_and_task():
    closed = view(goals={1: replace(GOAL, status="closed")})
    assert RULES.reconcile(use_case(), closed) == Transition(100, "cancelled", "cancelled", reason="goal_closed")
    paused = view(goals={1: replace(GOAL, status="paused")})
    assert RULES.reconcile(build(), paused).reason == "goal_paused"
    ended = view(claims={21: replace(CLAIM, status="invalidated")})
    assert RULES.reconcile(build("running", None, run_id=5), ended).reason == "claim_invalidated"
    retired = view(claims={21: replace(CLAIM, status="retired")})
    assert RULES.reconcile(use_case(), retired).reason == "claim_retired"
    done_task = view(tasks={300: TaskView(300, 1, "done")})
    queued = build("queued", None, task_id=300)
    assert RULES.reconcile(queued, done_task) == Transition(
        200, "cancelled", "cancelled", reason="task_closed", detail={"group": "done"}
    )
    # a build whose run is out keeps going: the run finishes on its own
    assert RULES.reconcile(build("running", None, task_id=300, run_id=5), done_task) is None
    assert RULES.reconcile(replace(queued, state="done"), closed) is None  # a finished one never moves


def test_loop_rules_a_build_waits_for_its_frame_task_and_its_split_then_queues():
    item = build()
    assert RULES.reconcile(item, view()) is None  # the plugin srs has not handled the approval yet
    approval = {42: ApprovalView(42, 600, False)}
    task = TaskView(300, 1, "todo")
    found = RULES.reconcile(item, view(approvals=approval, frames={FRAME: task}))
    assert found == Transition(200, "watching", "waiting", reason="split", task_id=300, detail={"task": 1})
    split = build(reason="split", task_id=300)
    assert RULES.reconcile(split, view(approvals=approval, tasks={300: task})) is None
    ready = {42: ApprovalView(42, 600, True)}
    assert RULES.reconcile(split, view(approvals=ready, tasks={300: task})) == Transition(
        200, "queued", "ready", task_id=300, detail={"task": 1}
    )
    assert RULES.reconcile(split, view(approvals=ready, tasks={300: replace(task, group="doing")})) is None
    sprints = view(policy={**POLICY, "require_sprint": True}, approvals=ready, tasks={300: task})
    assert RULES.reconcile(split, sprints) is None
    assert RULES.reconcile(split, replace(sprints, tasks={300: replace(task, sprint_active=True)})).state == "queued"
    assert RULES.reconcile(item, view(approvals=ready)) == Transition(
        200, "cancelled", "cancelled", reason="no_frame_task", detail={"frame": FRAME}
    )


def test_loop_rules_runs_end_a_build_with_its_plan_and_a_deliver_with_its_merge_requests():
    running = build("running", None, task_id=300, run_id=700, srs_document_id=5, use_case="UC ORD-1", srs_version=2)
    tasks = {300: TaskView(300, 1, "doing")}
    assert RULES.reconcile(running, view(tasks=tasks, runs={700: RunView(700, "author", "running")})) is None
    wrote = view(tasks=tasks, runs={700: RunView(700, "author", "done", "northwind-reorder")})
    done = RULES.reconcile(running, wrote)
    assert (done.state, done.plan_id, done.detail) == ("done", "northwind-reorder", {"plan": "northwind-reorder"})
    (deliver,) = done.spawn
    assert (deliver.kind, deliver.key, deliver.parent_id, deliver.task_id) == (
        "deliver",
        "deliver:northwind-reorder",
        200,
        300,
    )
    nothing = view(tasks=tasks, runs={700: RunView(700, "author", "done")})
    assert RULES.reconcile(running, nothing) == Transition(
        200,
        "queued",
        "failed",
        detail={"run_id": 700, "why": "the author run ended without a plan", "failures": 1},
        failed=True,
    )
    lost = RULES.reconcile(running, view(tasks=tasks, runs={700: RunView(700, "author", "lost")}))
    assert (lost.state, lost.failed, lost.detail) == (
        "queued",
        True,
        {"run_id": 700, "run_state": "lost", "failures": 1},
    )
    stopped = RULES.reconcile(running, view(tasks=tasks, runs={700: RunView(700, "author", "cancelled")}))
    assert (stopped.state, stopped.reason) == ("cancelled", "person")

    item = ItemView(800, "deliver", "deliver:northwind-reorder", "running", None, 1, plan_id="northwind-reorder")
    item = replace(item, run_id=900)
    half = view(runs={900: RunView(900, "plan", "done", "northwind-reorder", False)})
    assert RULES.reconcile(item, half).failed
    full = view(runs={900: RunView(900, "plan", "done", "northwind-reorder", True)})
    assert RULES.reconcile(item, full) == Transition(
        800, "watching", "waiting", reason="merge_queue", detail={"run_id": 900, "repos": []}
    )
    watching = replace(item, state="watching", reason="merge_queue")
    at = NOW - timedelta(minutes=5)
    shop = MergeView(1, "northwind-shop", "done", "abc123", at, True)
    docs = MergeView(2, "lumen.docs", "verifying", None, at, True)
    assert RULES.reconcile(watching, view(merges={"northwind-reorder": (shop, docs)})) is None
    unchecked = replace(docs, state="merged", after_sha="def456", checked=False)
    assert RULES.reconcile(watching, view(merges={"northwind-reorder": (shop, unchecked)})) == Transition(
        800, "done", "done", detail={"after_sha": ["abc123", "def456"], "released_at": at.isoformat()}
    )
    reverted = replace(docs, state="reverted")
    assert RULES.reconcile(watching, view(merges={"northwind-reorder": (shop, reverted)})).failed


def test_loop_rules_a_merge_request_a_person_merged_on_gitlab_counts_as_through():
    """A merge request held at a gate that a person merges by hand on GitLab leaves the queue (closed); the deliver
    counts it through when the queue's ledger says GitLab merged it, and waits on one closed without a merge."""
    item = ItemView(800, "deliver", "deliver:northwind-reorder", "waiting_human", "merge_blocked", 1)
    held = replace(item, plan_id="northwind-reorder", detail={"merges": [2]})
    at = NOW - timedelta(minutes=5)
    shop = MergeView(1, "northwind-shop", "done", "abc123", at, True)
    by_hand = MergeView(2, "lumen.docs", "closed", "fed789", at, True, outside=True)
    assert RULES.reconcile(held, view(merges={"northwind-reorder": (shop, by_hand)})) == Transition(
        800, "done", "done", detail={"after_sha": ["abc123", "fed789"], "released_at": at.isoformat()}
    )
    closed = replace(by_hand, after_sha=None, merged_at=None, outside=False)  # closed on GitLab, never merged
    assert RULES.reconcile(held, view(merges={"northwind-reorder": (shop, closed)})) == Transition(
        800, "watching", "waiting", reason="merge_queue", detail={}
    )


def test_loop_rules_replaying_a_chain_returns_the_same_keys():
    """The engine is a function of the event and the view: the same chain gives the same work items, whose keys
    hold no id of a row of hub_events."""
    chain = [baseline(moved("testing")), approved(), Event(62, "tasks.status", {"v": 1}, NOW)]
    seen = view(items=(use_case(),), tasks_on=True)

    def keys():
        return [effect.key for event in chain for effect in RULES.rules(event, seen) if isinstance(effect, WorkSpec)]

    first = keys()
    assert first == ["use_case:northwind-pantry:CL-1:b4", f"build:{FRAME}"]
    assert keys() == first
    renumbered = [replace(event, id=event.id + 1000) for event in chain]
    again = [e.key for event in renumbered for e in RULES.rules(event, seen) if isinstance(e, WorkSpec)]
    assert again == first
