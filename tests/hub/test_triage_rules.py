"""The frame of the agent of a triage thread as pure rules (``evo_agents.hub.triage_rules``): what an edit of a plan may
change, and when a chain of reruns is full. Criterion a5 and a7 of the plan run-failure-triage."""

from __future__ import annotations

import copy

import pytest

from evo_agents.hub import triage_rules as rules

REPOS = {"evo-agents"}


def plan() -> dict:
    return {
        "id": "rollout",
        "goal": "Ship it.",
        "acceptance": [{"id": "a1", "criterion": "it ships", "verify": "make check"}],
        "non_goals": ["no rewrite"],
        "steps": [
            {"id": 1, "repo": "evo-agents", "what": "the model", "status": "done", "verify": "pytest -q"},
            {"id": 2, "repo": "evo-agents", "what": "the queue and its tests", "status": "pending", "verify": "a && b"},
            {"id": 3, "repo": "evo-web", "what": "the page", "status": "pending", "verify": "pnpm test"},
        ],
    }


def edited(**change) -> dict:
    new = copy.deepcopy(plan())
    for key, value in change.items():
        if key.startswith("step"):
            new["steps"][int(key[4]) - 1].update(value)
        else:
            new[key] = value
    return new


def test_narrowing_the_what_or_changing_the_verify_of_a_pending_step_of_the_repo_is_allowed():
    assert rules.edit_problems(plan(), edited(step2={"what": "the queue", "verify": "a && b && c"}), REPOS) == []
    assert rules.edit_problems(plan(), edited(step2={"verify": "x && y"}), REPOS) == []
    assert rules.edit_problems(plan(), plan(), REPOS) == []


@pytest.mark.parametrize(
    ("change", "said"),
    [
        ({"step1": {"verify": "pytest -q -x"}}, "only a pending step"),
        ({"step1": {"what": "m"}}, "only a pending step"),
        ({"step1": {"status": "pending"}}, "only a pending step"),
        ({"step3": {"what": "page"}}, "repo evo-web"),
        ({"step2": {"status": "done"}}, "status is not the agent's"),
        ({"step2": {"title": "new"}}, "title is not one of verify, what"),
        ({"step2": {"what": "the queue and its tests, widened"}}, "only narrowed"),
        ({"step2": {"verify": "a"}}, "dropping a check"),
        ({"step2": {"verify": ""}}, "dropping a check"),
        ({"goal": "Ship more."}, "goal of the plan"),
        ({"acceptance": []}, "acceptance of the plan"),
        ({"non_goals": []}, "non_goals of the plan"),
    ],
)
def test_anything_else_is_refused_and_says_why(change, said):
    problems = rules.edit_problems(plan(), edited(**change), REPOS)
    assert any(said in line for line in problems), problems


def test_steps_are_neither_added_nor_removed():
    new = plan()
    new["steps"].pop()
    assert "not the agent's to add or remove" in rules.edit_problems(plan(), new, REPOS)[0]


def test_a_verify_counts_its_commands_not_its_characters():
    assert rules.clauses("a && b; c\nd") == ["a", "b", "c", "d"]
    assert rules.edit_problems(plan(), edited(step2={"verify": "a; b"}), REPOS) == []


def test_the_chain_holds_two_reruns_and_one_for_each_cause():
    assert rules.chain_problem(0, [], "network") is None
    assert rules.chain_problem(1, ["missing_tool"], "network") is None
    assert "once already for cause network" in rules.chain_problem(1, ["network"], "network")
    assert "the most (2)" in rules.chain_problem(2, ["network", "runtime"], "hub_5xx")


def test_a_cause_without_a_policy_a_guess_and_a_verify_alone_are_not_rerun():
    ok = {"inferred": False, "confirmed": False, "edited": False}
    assert rules.rerun_problem("network", **ok) is None
    assert "no automatic rerun" in rules.rerun_problem("credentials", **ok)
    assert "no automatic rerun" in rules.rerun_problem(None, **ok)
    assert "only together with an edit" in rules.rerun_problem("verify_failed", **ok)
    assert rules.rerun_problem("verify_failed", **{**ok, "edited": True}) is None
    assert "--confirm-cause" in rules.rerun_problem("network", inferred=True, confirmed=False, edited=False)
    assert rules.rerun_problem("network", inferred=True, confirmed=True, edited=False) is None
