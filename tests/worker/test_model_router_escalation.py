"""The Token strategy's rules and the worker's side of them (plan token-and-quota-economy, criterion a3): the rating
with no model call, the escalation after a failed easy or medium run, the strategies under the quota, and the worker
taking the hub's choice without a scorer, or scoring a run the hub left to it and taking its leases again. None of these
needs a database; the claim and the leases on the hub are in ``tests/hub/test_token_strategy_routing.py``."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("aiohttp", reason="needs the worker extra, evo-ak[worker]")

from evo_agents.hub import model_router
from evo_agents.hub.model_router import Quota
from evo_agents.worker import model_router as worker_router
from evo_agents.worker.runner.agent import Agent
from tests.worker.test_model_router import scorer_env  # noqa: F401  the fake claude on PATH

ROUTING = {
    "strategy": "surplus-first",
    "hard_reserve_pct": 10,
    "five_hour_ceiling_pct": 90,
    "ladder": {"easy": "haiku", "medium": "sonnet", "hard": "opus"},
    "overflow": {"easy": "glm/glm-4.6", "medium": "glm/glm-4.6", "hard": None},
}
SPARE = Quota(weekly_left=0.8, five_hour_used=0.2, known=True)
RESERVE = Quota(weekly_left=0.05, five_hour_used=0.2, known=True)
HOT = Quota(weekly_left=0.8, five_hour_used=0.95, known=True)
SPENT = Quota(weekly_left=0.0, five_hour_used=1.0, known=True)


# Rating


def step(**fields) -> dict:
    return {"id": 3, "title": "Queue", "what": "the queue", "acceptance": ["a1"], **fields}


@pytest.mark.parametrize(
    ("fields", "difficulty", "source"),
    [
        ({"difficulty": "easy", "what": "a schema migration"}, "easy", "plan"),
        ({"what": "add the column and its migration"}, "hard", "rule"),
        ({"what": "close the security hole of the login"}, "hard", "rule"),
        ({"what": "update docs/hub.md and README.md"}, "easy", "rule"),
        ({"what": "update docs/hub.md and evo_agents/hub/plans.py"}, "medium", "rule"),
        ({"what": "the queue", "verify": "test -f feature.txt"}, "medium", "rule"),
        ({"acceptance": None}, "hard", "rule"),
        ({}, "medium", "rule"),
    ],
)
def test_token_strategy_rates_a_step_with_no_model_call(fields, difficulty, source):
    rated = model_router.rate_step(step(**fields), {"steps": []})
    assert (rated.difficulty, rated.source) == (difficulty, source)


def test_token_strategy_rates_the_plans_last_step_that_merges_hard():
    last = step(what="run the tests of the diff, then merge and deploy")
    assert model_router.rate_step(last, {"steps": [step(), last]}).difficulty == "hard"
    assert model_router.rate_step(last, {"steps": [last, step()]}).difficulty == "medium"


@pytest.mark.parametrize(
    ("kind", "prompt", "role", "difficulty"),
    [
        ("plan", "anything", None, "hard"),
        ("author", "a plan for the export", None, "medium"),
        ("author", "redesign the auth architecture", None, "hard"),
        ("question", "Where is the claim written?", None, "easy"),
        ("task", "fix the typo in the README", None, "easy"),
        ("task", "fix the typo in worker/agent.py", None, None),
        ("chat", "look at the failing build", "lumen-architect", "hard"),
        ("chat", "look at the failing build", "Northwind docs writer", "easy"),
        ("task", "look at the failing build", None, None),
    ],
)
def test_token_strategy_rates_a_free_prompt_and_leaves_the_rest_to_the_scorer(kind, prompt, role, difficulty):
    assert model_router.rate_prompt(kind, prompt, role).difficulty == difficulty


def test_token_strategy_escalates_one_level_after_a_failed_easy_or_medium_run():
    assert [model_router.escalate(level) for level in ("easy", "medium", "hard", None)] == [
        "medium",
        "hard",
        "hard",
        "medium",
    ]
    assert model_router.at_least("easy", "medium") == "medium"
    assert model_router.at_least("hard", "medium") == "hard"
    assert model_router.at_least("easy", None) == "easy"


# Choosing


@pytest.mark.parametrize(
    ("strategy", "difficulty", "quota", "model", "branch"),
    [
        ("surplus-first", "easy", SPARE, "haiku", "ladder"),
        ("surplus-first", "easy", None, "haiku", "ladder"),  # no account reported: counts unused
        ("surplus-first", "easy", RESERVE, "glm/glm-4.6", "overflow"),
        ("surplus-first", "medium", HOT, "glm/glm-4.6", "overflow"),
        ("surplus-first", "hard", RESERVE, "opus", "ladder"),  # the reserve is kept for hard work
        ("surplus-first", "hard", SPENT, "opus", "ladder"),  # the overflow names no hard model
        ("cost-first", "easy", SPARE, "glm/glm-4.6", "overflow"),
        ("cost-first", "hard", SPARE, "opus", "ladder"),
        ("quota-burn", "easy", SPARE, "opus", "ladder"),
        ("quota-burn", "easy", RESERVE, "glm/glm-4.6", "overflow"),
    ],
)
def test_token_strategy_chooses_by_the_strategy_and_the_quota(strategy, difficulty, quota, model, branch):
    choice = model_router.choose({**ROUTING, "strategy": strategy}, difficulty, quota)
    assert (choice.model, choice.branch) == (model, branch)


def test_token_strategy_record_keeps_the_hubs_decision_and_leaves_out_what_is_null():
    found = model_router.record(
        "haiku", "easy", "haiku", source="rule", reason="r" * 999, branch="ladder", gateway=None
    )
    assert (found["scored"], found["source"], len(found["reason"])) == (False, "rule", model_router.MAX_REASON_CHARS)
    assert "gateway" not in found


# The worker


HUB_RATED = {"scorer": "haiku", "ladder": dict.fromkeys(("easy", "medium", "hard"), "glm/glm-4.6"),
             "difficulty": "easy", "source": "rule", "reason": "the step changes documents alone",
             "strategy": "cost-first", "branch": "overflow", "escalated_from": None}  # fmt: skip
PENDING = {"scorer": "haiku", "ladder": {"easy": "glm/glm-4.6", "medium": "sonnet", "hard": "opus"}, "pending": True,
           "source": "scorer", "reason": "left to the scorer", "strategy": "cost-first"}  # fmt: skip


def test_token_strategy_worker_takes_the_hubs_choice_without_a_scorer(scorer_env, tmp_path):
    found = asyncio.run(
        worker_router.route({"routing": HUB_RATED, "prompt": "p"}, {**scorer_env, "FAKE_SCORER": "hard"})
    )
    assert (found["difficulty"], found["model"], found["scored"], found["source"]) == ("easy", "glm/glm-4.6", False,
                                                                                      "rule")  # fmt: skip
    assert not (tmp_path / "log.json").exists(), "no scorer ran"


def test_token_strategy_worker_scores_a_run_the_hub_left_to_it(scorer_env):
    found = asyncio.run(worker_router.route({"routing": PENDING, "prompt": "p"}, {**scorer_env, "FAKE_SCORER": "easy"}))
    assert (found["difficulty"], found["model"], found["scored"], found["pending"]) == ("easy", "glm/glm-4.6", True,
                                                                                         False)  # fmt: skip
    failed = asyncio.run(worker_router.route({"routing": PENDING, "prompt": "p"}, {"PATH": "/nonexistent"}))
    assert (failed["model"], failed["source"]) == ("sonnet", "fallback")


def _agent(routing: dict, found_calls: list) -> Agent:
    async def report(state, **fields):
        found_calls.append(("report", state, fields))
        return {}

    async def swap():
        found_calls.append(("swap",))
        return True

    run = SimpleNamespace(
        id=7,
        spec={"routing": routing, "prompt": "p"},
        runtime="claude-code",
        state="running",
        reports=SimpleNamespace(report=report),
        credentials=SimpleNamespace(swap=swap),
        notes=[],
        note=lambda text, **_: None,
    )
    return Agent(run)


def test_token_strategy_worker_takes_its_leases_again_after_the_scorer_picks_a_gateway_model():
    calls: list = []
    found = model_router.record("haiku", "easy", "glm/glm-4.6", pending=False)
    asyncio.run(_agent(PENDING, calls).reroute_leases(found))
    assert calls == [("report", "running", {"routing": found}), ("swap",)]

    calls.clear()  # the hub chose the model: the leases of the claim are already the model's
    asyncio.run(_agent(HUB_RATED, calls).reroute_leases(found))
    native = {**PENDING, "ladder": {"easy": "haiku", "medium": "sonnet", "hard": "opus"}}
    asyncio.run(_agent(native, calls).reroute_leases(found))
    assert calls == []
