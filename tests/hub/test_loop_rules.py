"""The engines of the Loop, rules/1 (``evo_agents.hub.server.loop.rules``) and rules/2 (``rules2``, the engine of this
release), as criterion a4 of plan loop-engine, the table of rules of the report's section Loop and criterion a6 of
plan problem-oriented-hub name them: what each delta makes, its idempotent key, and where each work item moves, stale
ones cancelled. Every test of rules/1 runs on both engines with a claim of the Solution cell in a project of use
cases, where rules/2 does what rules/1 does; the tests of rules/2 after them hold what it changes: a claim's cell
decides its work (a use case or a build of a solution, a discover of a problem, nothing for the other cells), and each
work item carries the problem its goal answers. The engines are pure: these tests hand them events and views and read
what they return, with no Postgres. The canvas, Northwind Pantry, and the SRS document demo-shop are made up.
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("sqlalchemy")

from evo_agents.hub.autonomy import effective
from evo_agents.hub.server.loop import model, rules, rules2
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
from evo_agents.hub.server.loop.tick import tick
from evo_agents.hub.tables_srs import frame_task_key

NOW = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
POLICY = effective({"loop": {"enabled": True}}, {})["loop"]
GOAL = GoalView(1, "G-1", 3, "active", True, False, 10, 1, 7)
CLAIM = ClaimView(21, 10, "northwind-pantry", "CL-1", "testing", ("UC ORD-1",), cell="solution")
FRAME = frame_task_key("demo-shop", 2, "UC ORD-1")
ENGINES = {"rules/1": rules.RULES, "rules/2": rules2.RULES}


@pytest.fixture(params=sorted(ENGINES))
def engine(request):
    """Each engine: rules/2 does what rules/1 does for a claim of the Solution cell in a project of use cases."""
    return ENGINES[request.param]


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


def test_loop_rules_a_claim_moved_to_testing_makes_one_use_case_keyed_by_its_baseline(engine):
    (spec,) = engine.rules(baseline(moved("testing")), view())
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
    assert engine.rules(baseline(moved("testing"), event_id=51), view()) == [spec]
    # a claim of a canvas no active goal with a metric drives makes nothing
    assert engine.rules(baseline(moved("testing")), view(goals={1: replace(GOAL, canvas_id=11)})) == []
    assert engine.rules(baseline(moved("testing")), view(goals={1: replace(GOAL, has_metric=False)})) == []
    assert engine.rules(baseline(moved("testing")), view(goals={1: replace(GOAL, status="paused")})) == []
    # the project's one active goal with a metric and no canvas takes every canvas
    (spec,) = engine.rules(baseline(moved("testing")), view(goals={1: replace(GOAL, canvas_id=None)}))
    assert spec.goal_id == GOAL.id


def test_loop_rules_a_use_case_waits_out_the_stable_hours_of_the_policy(engine):
    stable = view(policy={**POLICY, "stable_hours": 2})
    (spec,) = engine.rules(baseline(moved("testing"), at=NOW - timedelta(hours=1)), stable)
    assert (spec.state, spec.reason, spec.detail["stable_at"]) == (
        "watching",
        "stable",
        (NOW + timedelta(hours=1)).isoformat(),
    )
    waiting = use_case("watching", reason="stable", detail=spec.detail)
    assert engine.reconcile(waiting, stable) is None
    later = replace(stable, now=NOW + timedelta(hours=1))
    assert engine.reconcile(waiting, later) == Transition(100, "queued", "ready")
    (spec,) = engine.rules(baseline(moved("testing"), at=NOW - timedelta(hours=3)), stable)
    assert (spec.state, spec.reason) == ("queued", None)


def test_loop_rules_validated_and_invalidated_are_noted_in_the_chain_and_make_nothing(engine):
    assert engine.rules(baseline(moved("validated", before="testing")), view()) == []  # no chain yet: nothing to note
    chain = view(items=(use_case("done"), build("queued", None, task_id=300)), tasks={300: TaskView(300, 1, "todo")})
    (note,) = engine.rules(baseline(moved("validated", before="testing")), chain)
    assert note == Note(
        21,
        "noted:northwind-pantry:CL-1:b4",
        {"status": "validated", "canvas": "northwind-pantry", "claim": "CL-1", "baseline": 4},
    )
    (ended,) = engine.rules(baseline(moved("invalidated", before="testing")), chain)
    assert (ended.comment_on, ended.author_id) == ((300,), GOAL.owner_id)
    assert "CL-1 of canvas northwind-pantry is invalidated since baseline 4" in ended.comment
    closed = replace(chain, tasks={300: TaskView(300, 1, "done")})
    (retired,) = engine.rules(baseline(moved("retired", before="testing")), closed)
    assert (retired.comment, retired.comment_on) == (None, ())  # no open frame task to tell


def test_loop_rules_an_approved_version_of_a_use_case_closes_it_and_makes_a_build_per_new_use_case(engine):
    case = use_case("running", run_id=500, runs=(499, 500))
    found = engine.rules(
        approved(added=("UC ORD-1", "UC ORD-2")), view(items=(case,), versions={42: 499}, tasks_on=True)
    )
    done, first, second = found
    assert done == Transition(100, "done", "done", detail={"document": "demo-shop", "version": 2})
    assert (first.key, first.state, first.reason, first.parent_id) == (f"build:{FRAME}", "watching", "auto_tasks", 100)
    assert (first.claim_id, first.srs_document_id, first.use_case, first.srs_version) == (21, 5, "UC ORD-1", 2)
    assert first.detail == {"frame": FRAME, "version_id": 42}
    assert second.key == "build:" + frame_task_key("demo-shop", 2, "UC ORD-2")
    assert model.CYCLE_OF[first.kind] == "development"


def test_loop_rules_a_version_adding_a_use_case_a_claim_in_a_chain_links_answers_that_chain_alone(engine):
    linked = view(items=(use_case(),), tasks_on=True)
    done, spec = engine.rules(approved(added=("UC ORD-1", "UC RET-1")), linked)
    assert done.item_id == 100 and spec.use_case == "UC ORD-1"  # the claim links UC ORD-1 alone
    # a version that changes the use case closes the work item and makes no build: auto_tasks makes no frame
    assert engine.rules(approved(added=(), changed=("uc  ord-1",)), linked) == [
        Transition(100, "done", "done", detail={"document": "demo-shop", "version": 2})
    ]
    # outside every chain, or with the plugin tasks off, nothing is built
    assert engine.rules(approved(added=("UC SHIP-1",)), linked) == []
    assert engine.rules(approved(), replace(linked, tasks_on=False)) == [
        Transition(100, "done", "done", detail={"document": "demo-shop", "version": 2})
    ]
    assert engine.rules(approved(), view(tasks_on=True)) == []  # a claim with no chain


def test_loop_rules_other_events_make_nothing(engine):
    for kind in ("tasks.status", "canvas.claim", "signal.posted"):
        assert engine.rules(Event(70, kind, {"v": 1}, NOW), view(items=(use_case(), build()))) == []


# What the clock makes


def test_loop_rules_a_goal_gets_a_brainstorm_for_each_revision_then_each_week_one_at_a_time(engine):
    (spec,) = engine.scheduled(view())
    assert spec == WorkSpec("brainstorm", "brainstorm:G-1:r3", 1, owner="@strategist", detail={"revision": 3})
    keys = frozenset({"brainstorm:G-1:r3"})
    assert engine.scheduled(view(keys=keys)) == []  # its week has not come
    week_at = NOW - timedelta(hours=1)
    weekly = view(keys=keys, week="2026-W43", week_at=week_at, brainstormed={1: NOW - timedelta(days=3)})
    (spec,) = engine.scheduled(weekly)
    assert (spec.key, spec.detail) == ("brainstorm:G-1:2026-W43", {"week": "2026-W43"})
    assert engine.scheduled(replace(weekly, keys=keys | {spec.key})) == []
    assert engine.scheduled(replace(weekly, proposals={1: 2})) == []  # proposals of the last one still open
    assert engine.scheduled(replace(weekly, brainstormed={1: NOW})) == []  # one was made since the week's time
    running = ItemView(9, "brainstorm", "brainstorm:G-1:r2", "running", None, 1)
    assert engine.scheduled(view(items=(running,))) == []  # one at a time
    assert engine.scheduled(view(goals={1: replace(GOAL, reached=True)})) == []
    assert engine.scheduled(view(goals={1: replace(GOAL, reached=None)})) != []  # no signal yet: not met
    for goal in (replace(GOAL, has_metric=False), replace(GOAL, status="closed")):
        assert engine.scheduled(view(goals={1: goal})) == []


# Where a work item moves


def test_loop_rules_stale_work_items_are_cancelled_by_goal_claim_and_task(engine):
    closed = view(goals={1: replace(GOAL, status="closed")})
    assert engine.reconcile(use_case(), closed) == Transition(100, "cancelled", "cancelled", reason="goal_closed")
    paused = view(goals={1: replace(GOAL, status="paused")})
    assert engine.reconcile(build(), paused).reason == "goal_paused"
    ended = view(claims={21: replace(CLAIM, status="invalidated")})
    assert engine.reconcile(build("running", None, run_id=5), ended).reason == "claim_invalidated"
    retired = view(claims={21: replace(CLAIM, status="retired")})
    assert engine.reconcile(use_case(), retired).reason == "claim_retired"
    done_task = view(tasks={300: TaskView(300, 1, "done")})
    queued = build("queued", None, task_id=300)
    assert engine.reconcile(queued, done_task) == Transition(
        200, "cancelled", "cancelled", reason="task_closed", detail={"group": "done"}
    )
    # a build whose run is out keeps going: the run finishes on its own
    assert engine.reconcile(build("running", None, task_id=300, run_id=5), done_task) is None
    assert engine.reconcile(replace(queued, state="done"), closed) is None  # a finished one never moves


def test_loop_rules_a_build_waits_for_its_frame_task_and_its_split_then_queues(engine):
    item = build()
    assert engine.reconcile(item, view()) is None  # the plugin srs has not handled the approval yet
    approval = {42: ApprovalView(42, 600, False)}
    task = TaskView(300, 1, "todo")
    found = engine.reconcile(item, view(approvals=approval, frames={FRAME: task}))
    assert found == Transition(200, "watching", "waiting", reason="split", task_id=300, detail={"task": 1})
    split = build(reason="split", task_id=300)
    assert engine.reconcile(split, view(approvals=approval, tasks={300: task})) is None
    ready = {42: ApprovalView(42, 600, True)}
    assert engine.reconcile(split, view(approvals=ready, tasks={300: task})) == Transition(
        200, "queued", "ready", task_id=300, detail={"task": 1}
    )
    assert engine.reconcile(split, view(approvals=ready, tasks={300: replace(task, group="doing")})) is None
    sprints = view(policy={**POLICY, "require_sprint": True}, approvals=ready, tasks={300: task})
    assert engine.reconcile(split, sprints) is None
    assert engine.reconcile(split, replace(sprints, tasks={300: replace(task, sprint_active=True)})).state == "queued"
    assert engine.reconcile(item, view(approvals=ready)) == Transition(
        200, "cancelled", "cancelled", reason="no_frame_task", detail={"frame": FRAME}
    )


def test_loop_rules_runs_end_a_build_with_its_plan_and_a_deliver_with_its_merge_requests(engine):
    running = build("running", None, task_id=300, run_id=700, srs_document_id=5, use_case="UC ORD-1", srs_version=2)
    tasks = {300: TaskView(300, 1, "doing")}
    assert engine.reconcile(running, view(tasks=tasks, runs={700: RunView(700, "author", "running")})) is None
    wrote = view(tasks=tasks, runs={700: RunView(700, "author", "done", "northwind-reorder")})
    done = engine.reconcile(running, wrote)
    assert (done.state, done.plan_id, done.detail) == ("done", "northwind-reorder", {"plan": "northwind-reorder"})
    (deliver,) = done.spawn
    assert (deliver.kind, deliver.key, deliver.parent_id, deliver.task_id) == (
        "deliver",
        "deliver:northwind-reorder",
        200,
        300,
    )
    nothing = view(tasks=tasks, runs={700: RunView(700, "author", "done")})
    assert engine.reconcile(running, nothing) == Transition(
        200,
        "queued",
        "failed",
        detail={"run_id": 700, "why": "the author run ended without a plan", "failures": 1},
        failed=True,
    )
    lost = engine.reconcile(running, view(tasks=tasks, runs={700: RunView(700, "author", "lost")}))
    assert (lost.state, lost.failed, lost.detail) == (
        "queued",
        True,
        {"run_id": 700, "run_state": "lost", "failures": 1},
    )
    stopped = engine.reconcile(running, view(tasks=tasks, runs={700: RunView(700, "author", "cancelled")}))
    assert (stopped.state, stopped.reason) == ("cancelled", "person")

    item = ItemView(800, "deliver", "deliver:northwind-reorder", "running", None, 1, plan_id="northwind-reorder")
    item = replace(item, run_id=900)
    half = view(runs={900: RunView(900, "plan", "done", "northwind-reorder", False)})
    assert engine.reconcile(item, half).failed
    full = view(runs={900: RunView(900, "plan", "done", "northwind-reorder", True)})
    assert engine.reconcile(item, full) == Transition(
        800, "watching", "waiting", reason="merge_queue", detail={"run_id": 900, "repos": []}
    )
    watching = replace(item, state="watching", reason="merge_queue")
    at = NOW - timedelta(minutes=5)
    shop = MergeView(1, "northwind-shop", "done", "abc123", at, True)
    docs = MergeView(2, "lumen.docs", "verifying", None, at, True)
    assert engine.reconcile(watching, view(merges={"northwind-reorder": (shop, docs)})) is None
    unchecked = replace(docs, state="merged", after_sha="def456", checked=False)
    assert engine.reconcile(watching, view(merges={"northwind-reorder": (shop, unchecked)})) == Transition(
        800, "done", "done", detail={"after_sha": ["abc123", "def456"], "released_at": at.isoformat()}
    )
    reverted = replace(docs, state="reverted")
    assert engine.reconcile(watching, view(merges={"northwind-reorder": (shop, reverted)})).failed


def test_loop_rules_a_merge_request_a_person_merged_on_gitlab_counts_as_through(engine):
    """A merge request held at a gate that a person merges by hand on GitLab leaves the queue (closed); the deliver
    counts it through when the queue's ledger says GitLab merged it, and waits on one closed without a merge."""
    item = ItemView(800, "deliver", "deliver:northwind-reorder", "waiting_human", "merge_blocked", 1)
    held = replace(item, plan_id="northwind-reorder", detail={"merges": [2]})
    at = NOW - timedelta(minutes=5)
    shop = MergeView(1, "northwind-shop", "done", "abc123", at, True)
    by_hand = MergeView(2, "lumen.docs", "closed", "fed789", at, True, outside=True)
    assert engine.reconcile(held, view(merges={"northwind-reorder": (shop, by_hand)})) == Transition(
        800, "done", "done", detail={"after_sha": ["abc123", "fed789"], "released_at": at.isoformat()}
    )
    closed = replace(by_hand, after_sha=None, merged_at=None, outside=False)  # closed on GitLab, never merged
    assert engine.reconcile(held, view(merges={"northwind-reorder": (shop, closed)})) == Transition(
        800, "watching", "waiting", reason="merge_queue", detail={}
    )


def test_loop_rules_a_deliver_on_github_is_done_once_its_plan_marks_its_repo_merged(engine):
    """A repo on GitHub is one the queue does not merge (``View.manual``): the deliver whose plan run is done waits for
    a person (``merge_manual``) while its plan does not mark the repo merged, and is done once it does, at the end of
    its run (the tick marked the pull request the run merged) or while it waits."""
    item = replace(
        ItemView(800, "deliver", "deliver:northwind-reorder", "running", None, 1, plan_id="northwind-reorder"),
        run_id=900,
    )
    runs = {900: RunView(900, "plan", "done", "northwind-reorder", True, ("evo-agents",))}
    manual, merged = frozenset({"evo-agents"}), {"northwind-reorder": frozenset({"evo-agents"})}
    detail = {"run_id": 900, "repos": ["evo-agents"]}
    assert engine.reconcile(item, view(runs=runs, manual=manual)) == Transition(
        800, "waiting_human", "waiting", reason="merge_manual", detail=detail
    )
    done = Transition(800, "done", "done", detail={"after_sha": [], "released_at": None})
    assert engine.reconcile(item, view(runs=runs, manual=manual, released=merged)) == done
    waiting = replace(item, state="waiting_human", reason="merge_manual", detail=detail)
    assert engine.reconcile(waiting, view(runs=runs, manual=manual)) is None, "merged by hand, not marked: it waits"
    assert engine.reconcile(waiting, view(runs=runs, manual=manual, released=merged)) == done
    other = {"northwind-reorder": frozenset({"lumen.docs"})}
    assert engine.reconcile(item, view(runs=runs, manual=manual, released=other)).reason == "merge_manual"


def test_loop_rules_replaying_a_chain_returns_the_same_keys(engine):
    """The engine is a function of the event and the view: the same chain gives the same work items, whose keys
    hold no id of a row of hub_events."""
    chain = [baseline(moved("testing")), approved(), Event(62, "tasks.status", {"v": 1}, NOW)]
    seen = view(items=(use_case(),), tasks_on=True)

    def keys():
        return [effect.key for event in chain for effect in engine.rules(event, seen) if isinstance(effect, WorkSpec)]

    first = keys()
    assert first == ["use_case:northwind-pantry:CL-1:b4", f"build:{FRAME}"]
    assert keys() == first
    renumbered = [replace(event, id=event.id + 1000) for event in chain]
    again = [e.key for event in renumbered for e in engine.rules(event, seen) if isinstance(e, WorkSpec)]
    assert again == first


# rules/2: the Loop goes from the problem a goal answers

R1, R2 = rules.RULES, rules2.RULES
PROBLEM = 5  # the id of problem P-1 of the project, which the goal answers
CLAIM_BUILD = "build:claim:northwind-pantry:CL-1:b4"
DISCOVER = "discover:northwind-pantry:CL-1:b4"


def cell(name: str | None, **changes) -> View:
    """The view with claim CL-1 of cell ``name``."""
    return view(claims={CLAIM.id: replace(CLAIM, cell=name)}, **changes)


def claim_build(state: str = "queued", reason: str | None = None, **changes) -> ItemView:
    detail = {"baseline": 4, "from": "assumption", "claim": "CL-1", "canvas": "northwind-pantry"}
    item = ItemView(400, "build", CLAIM_BUILD, state, reason, GOAL.id, claim_id=CLAIM.id, detail=detail)
    return replace(item, **changes)


def discover(state: str = "queued", reason: str | None = None, **changes) -> ItemView:
    item = ItemView(500, "discover", DISCOVER, state, reason, GOAL.id, claim_id=CLAIM.id)
    return replace(item, **changes)


def test_loop_rules2_is_the_engine_of_this_release_and_rules1_stays():
    assert (model.ENGINE, R2.name, R1.name) == ("rules/2", "rules/2", "rules/1")
    assert inspect.signature(tick).parameters["rules"].default is R2
    assert model.CYCLE_OF["discover"] == "business" and model.OWNER_OF["discover"] == "@strategist"
    assert model.KINDS[:4] == ("brainstorm", "use_case", "build", "deliver"), "loop/1 only adds"
    assert R1.reconcile(discover("running", run_id=9), view(runs={9: RunView(9, "task", "done")})) is None


def test_loop_rules2_a_claim_moved_to_testing_makes_the_work_of_its_cell():
    detail = {"baseline": 4, "from": "assumption", "claim": "CL-1", "canvas": "northwind-pantry"}
    # a solution in a project of use cases: rules/1's use case
    for solution in ("solution", "unique_value_proposition"):
        (spec,) = R2.rules(baseline(moved("testing")), cell(solution))
        assert (spec.kind, spec.key, spec.owner) == ("use_case", "use_case:northwind-pantry:CL-1:b4", "@ba")
    # a solution in a project with no use cases: a build straight from the claim
    (spec,) = R2.rules(baseline(moved("testing")), cell("solution", use_cases=False))
    assert spec == WorkSpec("build", CLAIM_BUILD, GOAL.id, claim_id=21, detail=detail)
    assert model.CYCLE_OF[spec.kind] == "development"
    # a problem: a discover of @strategist, whatever the project's use cases
    for use_cases in (True, False):
        (spec,) = R2.rules(baseline(moved("testing")), cell("problem", use_cases=use_cases))
        assert spec == WorkSpec("discover", DISCOVER, GOAL.id, claim_id=21, owner="@strategist", detail=detail)
    # the other cells make no core work item: a core plugin's rule decides for them
    for other in ("customer_segments", "channels", "key_metrics", "revenue_streams", "unfair_advantage", None):
        assert R2.rules(baseline(moved("testing")), cell(other)) == []
        assert R2.rules(baseline(moved("testing")), cell(other, use_cases=False)) == []
    # a claim that serves no active goal with a metric makes nothing of any cell
    for name in ("solution", "problem"):
        unserved = cell(name, use_cases=False, goals={1: replace(GOAL, canvas_id=11)})
        assert R2.rules(baseline(moved("testing")), unserved) == []
    # validated is noted in the chain as rules/1 notes it
    chain = cell("problem", items=(discover("done"),))
    (note,) = R2.rules(baseline(moved("validated", before="testing")), chain)
    assert isinstance(note, Note) and note.key == "noted:northwind-pantry:CL-1:b4"


def test_loop_rules2_a_build_or_discover_of_a_claim_waits_out_the_stable_hours():
    stable = {**POLICY, "stable_hours": 2}
    for name, use_cases, made in (("solution", False, claim_build), ("problem", True, discover)):
        found = cell(name, use_cases=use_cases, policy=stable)
        (spec,) = R2.rules(baseline(moved("testing"), at=NOW - timedelta(hours=1)), found)
        assert (spec.state, spec.reason, spec.detail["stable_at"]) == (
            "watching",
            "stable",
            (NOW + timedelta(hours=1)).isoformat(),
        )
        waiting = made("watching", "stable", detail=spec.detail)
        assert R2.reconcile(waiting, found) is None
        assert R2.reconcile(waiting, replace(found, now=NOW + timedelta(hours=1))) == Transition(
            waiting.id, "queued", "ready"
        )


def test_loop_rules2_work_items_carry_the_problem_their_goal_answers():
    answers = {GOAL.id: replace(GOAL, problem_id=PROBLEM)}
    (brainstorm,) = R2.scheduled(view(goals=answers))
    assert (brainstorm.key, brainstorm.problem_id) == ("brainstorm:G-1:r3", PROBLEM)
    for name, use_cases in (("solution", True), ("solution", False), ("problem", True)):
        (spec,) = R2.rules(baseline(moved("testing")), cell(name, use_cases=use_cases, goals=answers))
        assert spec.problem_id == PROBLEM
    (spec,) = R2.rules(baseline(moved("testing")), cell("problem"))
    assert spec.problem_id is None  # a goal that answers no problem
    # the build of a use case carries the problem of the use case it comes of, else its goal's
    case = use_case("running", run_id=500, runs=(499, 500), problem_id=4)
    _, build_spec = R2.rules(approved(), view(items=(case,), versions={42: 499}, tasks_on=True, goals=answers))
    assert (build_spec.parent_id, build_spec.problem_id) == (100, 4)
    case = replace(case, problem_id=None)
    _, build_spec = R2.rules(approved(), view(items=(case,), versions={42: 499}, tasks_on=True, goals=answers))
    assert build_spec.problem_id == PROBLEM
    # the deliver of a build carries the build's problem; one of a build rules/1 made, its goal's
    wrote = {700: RunView(700, "author", "done", "northwind-reorder")}
    for problem, carried in ((4, 4), (None, PROBLEM)):
        running = claim_build("running", run_id=700, problem_id=problem)
        done = R2.reconcile(running, cell("solution", runs=wrote, goals=answers, items=(running,)))
        (deliver,) = done.spawn
        assert (deliver.key, deliver.parent_id, deliver.problem_id) == ("deliver:northwind-reorder", 400, carried)
    # rules/1 carries none
    (spec,) = R1.rules(baseline(moved("testing")), cell("solution", goals=answers))
    assert spec.problem_id is None


def test_loop_rules2_work_items_carry_the_opportunity_their_goal_answers():
    answers = {GOAL.id: replace(GOAL, opportunity_id=7)}
    (brainstorm,) = R2.scheduled(view(goals=answers))
    assert (brainstorm.key, brainstorm.opportunity_id, brainstorm.problem_id) == ("brainstorm:G-1:r3", 7, None)
    for name, use_cases in (("solution", True), ("solution", False), ("problem", True)):
        (spec,) = R2.rules(baseline(moved("testing")), cell(name, use_cases=use_cases, goals=answers))
        assert (spec.opportunity_id, spec.problem_id) == (7, None)
    (spec,) = R2.rules(baseline(moved("testing")), cell("problem"))
    assert spec.opportunity_id is None  # a goal that answers none
    # the build of a use case carries the opportunity of the use case it comes of, else its goal's
    case = use_case("running", run_id=500, runs=(499, 500), opportunity_id=8)
    _, build_spec = R2.rules(approved(), view(items=(case,), versions={42: 499}, tasks_on=True, goals=answers))
    assert (build_spec.parent_id, build_spec.opportunity_id) == (100, 8)
    case = replace(case, opportunity_id=None)
    _, build_spec = R2.rules(approved(), view(items=(case,), versions={42: 499}, tasks_on=True, goals=answers))
    assert build_spec.opportunity_id == 7
    # rules/1 carries none
    (spec,) = R1.rules(baseline(moved("testing")), cell("solution", goals=answers))
    assert spec.opportunity_id is None


def test_loop_rules2_a_claim_build_goes_to_its_author_run_and_a_deliver_of_its_plan():
    queued = claim_build()
    assert queued.from_claim and not build().from_claim
    # queued, it waits for admission alone: no frame task to wait for, none whose closing cancels it
    assert R2.reconcile(queued, cell("solution", use_cases=False)) is None
    running = claim_build("running", run_id=700)
    assert R2.reconcile(running, cell("solution", runs={700: RunView(700, "author", "running")})) is None
    wrote = cell("solution", runs={700: RunView(700, "author", "done", "northwind-reorder")})
    done = R2.reconcile(running, wrote)
    assert (done.state, done.plan_id, done.detail) == ("done", "northwind-reorder", {"plan": "northwind-reorder"})
    (deliver,) = done.spawn
    assert (deliver.kind, deliver.key, deliver.claim_id, deliver.task_id, deliver.use_case) == (
        "deliver",
        "deliver:northwind-reorder",
        21,
        None,
        None,
    )
    nothing = R2.reconcile(running, cell("solution", runs={700: RunView(700, "author", "done")}))
    assert (nothing.state, nothing.failed, nothing.detail["why"]) == (
        "queued",
        True,
        "the author run ended without a plan",
    )
    asking = R2.reconcile(running, cell("solution", runs={700: RunView(700, "author", "waiting")}))
    assert (asking.state, asking.reason) == ("waiting_human", "author_question")
    ended = view(claims={21: replace(CLAIM, status="invalidated")})
    assert R2.reconcile(running, ended).reason == "claim_invalidated"


def test_loop_rules2_a_discover_is_done_with_its_task_run_and_a_run_that_fails_is_a_failure():
    running = discover("running", run_id=900)
    seen = cell("problem")
    assert R2.reconcile(running, replace(seen, runs={900: RunView(900, "task", "running")})) is None
    assert R2.reconcile(running, replace(seen, runs={900: RunView(900, "task", "done", proposals=2)})) == Transition(
        500, "done", "done", detail={"run_id": 900, "proposals": 2}
    )
    failed = {900: RunView(900, "task", "failed")}
    first = R2.reconcile(running, replace(seen, runs=failed))
    assert (first.state, first.reason, first.failed) == ("queued", None, True)
    second = R2.reconcile(replace(running, failures=1), replace(seen, runs=failed))
    assert (second.state, second.reason) == ("watching", "triage")
    third = R2.reconcile(replace(running, failures=2), replace(seen, runs=failed))
    assert (third.state, third.reason) == ("waiting_human", "loop_decision")
    stopped = R2.reconcile(running, replace(seen, runs={900: RunView(900, "task", "cancelled")}))
    assert (stopped.state, stopped.reason) == ("cancelled", "person")
    assert R2.reconcile(discover(), view(goals={1: replace(GOAL, status="paused")})).reason == "goal_paused"
    retired = view(claims={21: replace(CLAIM, cell="problem", status="retired")})
    assert R2.reconcile(discover(), retired).reason == "claim_retired"
    assert R2.reconcile(discover(), seen) is None  # queued: admission hands its run out


def test_loop_rules2_replaying_a_chain_returns_the_same_keys():
    """rules/2 is a function of the event and the view too: a baseline that moves a solution and a problem to testing,
    read twice and with the events renumbered, gives the same keys, none holding an id of a row of hub_events."""
    problem = ClaimView(22, 10, "northwind-pantry", "CL-2", "testing", cell="problem")
    chain = [baseline(moved("testing"), moved("testing", 22, "CL-2")), Event(62, "tasks.status", {"v": 1}, NOW)]
    claims = {CLAIM.id: CLAIM, problem.id: problem}

    def keys(events, use_cases: bool):
        seen = view(claims=claims, use_cases=use_cases)
        return [e.key for event in events for e in R2.rules(event, seen) if isinstance(e, WorkSpec)]

    renumbered = [replace(event, id=event.id + 1000) for event in chain]
    expected = {
        False: [CLAIM_BUILD, "discover:northwind-pantry:CL-2:b4"],
        True: ["use_case:northwind-pantry:CL-1:b4", "discover:northwind-pantry:CL-2:b4"],
    }
    for use_cases, wanted in expected.items():
        assert keys(chain, use_cases) == keys(chain, use_cases) == keys(renumbered, use_cases) == wanted


# rules/2: a solution that needs no code is an act (plan problem-intake)

ACT = "act:northwind-pantry:CL-1:b4"


def act(state: str = "running", reason: str | None = None, **changes) -> ItemView:
    item = ItemView(600, "act", ACT, state, reason, GOAL.id, claim_id=CLAIM.id, task_id=31)
    return replace(item, **changes)


def acting(**changes) -> View:
    """The view with CL-1 a solution whose delivery is action."""
    return view(**{"claims": {CLAIM.id: replace(CLAIM, delivery="action")}, **changes})


def test_loop_rules2_a_solution_with_delivery_action_makes_an_act_whatever_the_project_uses():
    detail = {"baseline": 4, "from": "assumption", "claim": "CL-1", "canvas": "northwind-pantry"}
    for solution in ("solution", "unique_value_proposition"):
        for use_cases in (True, False):
            found = acting(use_cases=use_cases, claims={CLAIM.id: replace(CLAIM, cell=solution, delivery="action")})
            (spec,) = R2.rules(baseline(moved("testing")), found)
            assert spec == WorkSpec("act", ACT, GOAL.id, claim_id=21, detail=detail)
    assert model.CYCLE_OF["act"] == "business" and "act" in model.KINDS
    # delivery code, the default, goes the old way; another cell makes no act
    (spec,) = R2.rules(baseline(moved("testing")), cell("solution", use_cases=False))
    assert spec.kind == "build"
    assert [e.kind for e in R2.rules(baseline(moved("testing")), cell("problem")) if isinstance(e, WorkSpec)] == [
        "discover"
    ]
    problem = view(claims={CLAIM.id: replace(CLAIM, cell="problem", delivery="action")})
    assert [e.kind for e in R2.rules(baseline(moved("testing")), problem) if isinstance(e, WorkSpec)] == ["discover"]
    # rules/1 knows no delivery: the same claim is its use case
    (spec,) = R1.rules(baseline(moved("testing")), acting())
    assert spec.kind == "use_case"


def test_loop_rules2_an_act_waits_out_the_stable_hours_then_is_done_with_its_task():
    stable = {**POLICY, "stable_hours": 2}
    found = acting(policy=stable)
    (spec,) = R2.rules(baseline(moved("testing"), at=NOW - timedelta(hours=1)), found)
    assert (spec.state, spec.reason) == ("watching", "stable")
    waiting = act("watching", "stable", detail=spec.detail, task_id=None)
    assert R2.reconcile(waiting, found) is None
    assert R2.reconcile(waiting, replace(found, now=NOW + timedelta(hours=1))) == Transition(
        waiting.id, "queued", "ready"
    )
    tasks = {31: TaskView(31, 7, "doing")}
    assert R2.reconcile(act(), acting(tasks=tasks)) is None
    done = {31: TaskView(31, 7, "done")}
    assert R2.reconcile(act(), acting(tasks=done)) == Transition(600, "done", "done", detail={"task": 7})
    assert R2.reconcile(act("queued", task_id=None), acting()) is None  # admission makes its task


def test_loop_rules2_an_act_whose_task_is_cancelled_gone_or_overdue_is_a_failure_as_any_kind():
    today = NOW.date()
    for task in (TaskView(31, 7, "cancelled"), TaskView(31, 7, "doing", due_on=today - timedelta(days=1)), None):
        tasks = {} if task is None else {31: task}
        first = R2.reconcile(act(), acting(tasks=tasks))
        assert (first.state, first.reason, first.failed) == ("queued", None, True)
        assert first.detail["why"]
        second = R2.reconcile(act(failures=1), acting(tasks=tasks))
        assert (second.state, second.reason) == ("watching", "triage")
        third = R2.reconcile(act(failures=2), acting(tasks=tasks))
        assert (third.state, third.reason) == ("waiting_human", "loop_decision")
    due_today = {31: TaskView(31, 7, "doing", due_on=today)}
    assert R2.reconcile(act(), acting(tasks=due_today)) is None
    assert R2.reconcile(act(), acting(goals={1: replace(GOAL, status="paused")})).reason == "goal_paused"
    retired = view(claims={CLAIM.id: replace(CLAIM, delivery="action", status="retired")})
    assert R2.reconcile(act(), retired).reason == "claim_retired"


def test_loop_rules2_replaying_an_act_returns_the_same_key_and_old_work_items_read_as_before():
    chain = [baseline(moved("testing"))]
    keys = lambda events: [e.key for event in events for e in R2.rules(event, acting()) if isinstance(e, WorkSpec)]  # noqa: E731
    assert keys(chain) == keys(chain) == keys([replace(chain[0], id=1050)]) == [ACT]
    assert ClaimView(1, 2, "c", "CL-1", None).delivery == "code"  # a claim read with no delivery is a code claim
    assert model.KINDS[:5] == ("brainstorm", "use_case", "build", "deliver", "discover"), "loop/1 only adds"
