"""The checks of the Loop's admission and the caps of the runs it hands out (``evo_agents.hub.server.loop.admit``,
``caps``), as criterion a5 of plan loop-engine names them, with no Postgres: the order of the checks, the first that
fails being the work item's reason, each run handed out counting against the next, the repos of a plan a deliver
holds, and the turns and minutes of an author or a plan run with no cost cap. ``tests.hub.test_loop_admission`` runs
them in a tick. The repos and plans are made up.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace

import pytest

pytest.importorskip("sqlalchemy")

from evo_agents.hub.autonomy import effective, problems
from evo_agents.hub.server.loop.admit import Capacity, LoopWorker, open_repos, policy_selector, refusal
from evo_agents.hub.server.loop.caps import run_budget, timeout_s
from evo_agents.hub.server.loop.model import ItemView
from evo_agents.hub.server.runs.models import RunBudget

POLICY = effective({"loop": {"enabled": True, "worker": "studio"}}, {})["loop"]
STUDIO = LoopWorker("studio", 9, 7, free=2)


def item(kind: str, number: int = 1, plan_id: str | None = None) -> ItemView:
    return ItemView(number, kind, f"{kind}:{number}", "queued", None, 1, plan_id=plan_id)


def capacity(**changes) -> Capacity:
    return replace(Capacity(tripped=False, worker=STUDIO), **changes)


def policy(**loop) -> dict:
    return effective({"loop": {"enabled": True, "worker": "studio", **loop}}, {})["loop"]


def test_loop_admission_names_the_first_check_that_fails_breaker_cycle_rate_then_worker():
    build = item("build")
    worst = capacity(
        tripped=True,
        running=Counter(development=1),
        dispatched=6,
        worker=LoopWorker("studio", down="worker studio is offline"),
    )
    assert refusal(build, (), worst, POLICY) == ("breaker", {})
    worst = replace(worst, tripped=False)
    assert refusal(build, (), worst, POLICY) == (
        "concurrency",
        {"cycle": "development", "running": 1, "max_running": 1},
    )
    worst.running = Counter()
    assert refusal(build, (), worst, POLICY) == ("rate", {"dispatched": 6, "dispatches_per_hour": 6})
    worst.dispatched = 5
    assert refusal(build, (), worst, POLICY) == ("worker", {"worker": "studio", "why": "worker studio is offline"})
    assert refusal(build, (), capacity(), POLICY) is None


def test_loop_admission_a_cycle_at_zero_takes_nothing_and_a_task_run_needs_no_loop_worker():
    stopped = policy(business={"max_running": 0})
    assert refusal(item("brainstorm"), (), capacity(), stopped)[0] == "concurrency"
    nobody = capacity(worker=LoopWorker(None, down="the policy names no loop.worker"))
    for kind in ("brainstorm", "use_case"):  # their task runs go to the agent type's run_as
        assert refusal(item(kind), (), nobody, POLICY) is None
    for kind in ("build", "deliver"):
        assert refusal(item(kind), (), nobody, POLICY) == (
            "worker",
            {"worker": None, "why": "the policy names no loop.worker"},
        )
    assert refusal(item("brainstorm"), (), capacity(dispatched=0), policy(dispatches_per_hour=0))[0] == "rate"


def test_loop_admission_a_deliver_waits_while_a_repo_of_its_plan_is_held():
    held = capacity(repos=Counter({"northwind-shop": 1}))
    deliver = item("deliver", plan_id="pantry-alerts")
    assert refusal(deliver, ("northwind-api", "northwind-shop"), held, POLICY) == (
        "concurrency",
        {"repos": ["northwind-shop"], "repo_max_running": 1},
    )
    assert refusal(deliver, ("northwind-api",), held, POLICY) is None
    assert refusal(deliver, ("northwind-shop",), held, policy(repo_max_running=2)) is None
    assert refusal(item("build"), ("northwind-shop",), held, POLICY) is None  # the repo cap is a deliver's


def test_loop_admission_counts_each_run_it_hands_out_against_the_work_items_after_it():
    counted = capacity(worker=replace(STUDIO, free=1))
    counted.take(item("deliver", plan_id="pantry-alerts"), ("northwind-shop", "northwind-shop"))
    assert (counted.running, counted.repos, counted.dispatched) == (
        Counter(development=1),
        Counter({"northwind-shop": 1}),
        1,
    )
    assert counted.worker.free == 0 and counted.worker.problem == "worker studio has no free slot"
    counted.take(item("brainstorm"), ())
    assert counted.running == Counter(development=1, business=1) and counted.dispatched == 2
    assert counted.worker.free == 0  # a task run takes no slot of loop.worker
    assert refusal(item("build"), (), counted, policy(development={"max_running": 2}))[0] == "worker"


def test_loop_admission_reads_the_repos_of_the_steps_of_a_plan_not_done():
    body = {
        "repos": [{"repo": "northwind-shop", "branch": "loop/alerts"}, {"repo": "northwind-api"}],
        "steps": [
            {"id": 1, "repo": "northwind-docs", "status": "done"},
            {"id": 2, "repo": "northwind-shop", "status": "pending"},
            {"id": 3, "repo": "northwind-api"},
            {"id": 4, "repo": "northwind-shop", "status": "pending"},
        ],
    }
    assert open_repos(body) == ("northwind-shop", "northwind-api")
    alone = {"repos": [{"repo": "northwind-shop"}], "steps": [{"id": 1, "status": "pending"}]}
    assert open_repos(alone) == ("northwind-shop",)  # a step naming no repo: the plan's only one
    assert open_repos(None) == () and open_repos({"steps": "none"}) == ()


def test_loop_caps_author_and_plan_runs_carry_the_turns_and_minutes_of_the_policy_and_no_cost_cap():
    assert run_budget("build", POLICY) == {"max_usd": None, "max_turns": 200, "max_seconds": 3600}
    assert run_budget("deliver", POLICY) == {"max_usd": None, "max_turns": 600, "max_seconds": 14400}
    assert (timeout_s("build", POLICY), timeout_s("deliver", POLICY)) == (3900, 14700)
    tight = policy(caps={"author": {"minutes": 30, "turns": 50}, "plan": {"minutes": 90, "turns": 120}})
    assert run_budget("build", tight) == {"max_usd": None, "max_turns": 50, "max_seconds": 1800}
    assert run_budget("deliver", tight) == {"max_usd": None, "max_turns": 120, "max_seconds": 5400}
    assert timeout_s("deliver", tight) == 5700
    # the shape a claim hands the worker: Claude Code stops at the turns, Codex at the time, nothing at a cost
    assert RunBudget(**run_budget("build", tight)) == RunBudget(max_usd=None, max_turns=50, max_seconds=1800)


def test_loop_admission_loop_pool_and_loop_labels_make_the_selector_and_loop_worker_stays_without_them():
    assert policy_selector(POLICY) == ()
    assert policy_selector(policy(pool="ci", labels=["GPU", "zone=eu"])) == ("pool=ci", "gpu", "zone=eu")
    assert policy_selector(policy(labels=["gpu"])) == ("gpu",)
    nobody = capacity(worker=LoopWorker("pool=ci", down="no live worker holding pool=ci", selector=("pool=ci",)))
    assert refusal(item("build"), (), nobody, policy(pool="ci")) == (
        "worker",
        {"worker": "pool=ci", "why": "no live worker holding pool=ci"},
    )
    assert problems({"loop": {"pool": "ci", "labels": ["gpu"]}}, {}) == []
    assert problems({"loop": {"labels": ["no spaces"]}}, {})[0].startswith("loop.labels: a list of at most 10 labels")
