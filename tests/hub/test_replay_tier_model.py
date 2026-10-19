"""The rules of step 11 of plan history-driven-curator, without Postgres: when the replays of a change of the Curator
won (``evo_agents.hub.replay.verdict``: at least MIN_HOLDOUT holdout tasks with both arms at the change's head, arm b
passing more of them, or as many with a mean cost or turns at least MIN_SAVING lower), what edits a harness alone
(``replay.harness_only``), the tier 0 a change of a harness alone gets once its replays won (``tiers.replay_tier``),
and the sign of score hacking of an instruction to test or verify weakened in a skill, an AGENTS.md or a CLAUDE.md
(``judge.weakened``, ``judge.hack_signs``)."""

from __future__ import annotations

import pytest

from evo_agents.hub import judge, replay, tiers
from tests.hub.test_judge import file_diff, kinds, signs_of

HEAD = "b" * 40


def arm(passed: bool = True, *, cost: float | None = 1.0, turns: int | None = 10, sha: str = HEAD) -> dict:
    return {
        "passed": passed,
        "harness_sha": sha,
        "harness_paths": ["skills/demo/SKILL.md"],
        "cost_usd": cost,
        "turns": turns,
    }


def row(a: dict, b: dict, *, split: str = "holdout", state: str = "done") -> dict:
    return {"split": split, "state": state, "result_a": a, "result_b": b}


def rows(count: int, a: dict, b: dict, **extra) -> list[dict]:
    return [row(a, b, **extra) for _ in range(count)]


# The verdict of a change's replays


def test_replay_verdict_wins_with_as_many_passed_and_a_cost_or_turns_ten_percent_lower():
    found = replay.verdict(rows(6, arm(cost=1.0, turns=10), arm(cost=0.9, turns=10)), HEAD)
    assert found["won"] is True and found["tasks"] == 6 and "cost 10.0% lower" in found["reason"]
    assert found["a"] == {"passed": 6, "cost_usd": 1.0, "turns": 10.0} and found["saving"]["cost_usd"] == 0.1
    assert found["paths"] == ["skills/demo/SKILL.md"] and found["head_sha"] == HEAD
    turns = replay.verdict(rows(6, arm(cost=None, turns=20), arm(cost=None, turns=17)), HEAD)
    assert turns["won"] is True and "turns 15.0% lower" in turns["reason"] and turns["saving"]["cost_usd"] is None


def test_replay_verdict_wins_when_arm_b_passes_more_whatever_it_costs():
    found = replay.verdict([*rows(5, arm(), arm()), row(arm(False), arm(True, cost=5.0))], HEAD)
    assert (
        found["won"] is True and "arm b passed more of the 6 holdout tasks than arm a (6 against 5)" in found["reason"]
    )


@pytest.mark.parametrize(
    "replays, why",
    [
        (rows(6, arm(cost=1.0), arm(cost=0.95)), "with neither its mean cost nor its mean turns 10% lower"),
        ([*rows(5, arm(), arm(cost=0.1)), row(arm(True), arm(False))], "arm b passed fewer of the 6 holdout tasks"),
        (rows(5, arm(), arm(cost=0.1)), "5 holdout task(s) replayed with both arms at the head, of the 6 a win needs"),
        (rows(6, arm(), arm(cost=0.1), split="select"), "0 holdout task(s)"),  # the select split is never measured
        (rows(6, arm(), arm(cost=0.1, sha="c" * 40)), "0 holdout task(s)"),  # arm b ran another head than the merge's
        ([row(arm(), None, state="running")] * 6, "0 holdout task(s)"),
        ([], "0 holdout task(s)"),
    ],
)
def test_replay_verdict_loses_or_lacks_replays(replays, why):
    found = replay.verdict(replays, HEAD)
    assert found["won"] is False and why in found["reason"]


def test_replay_verdict_needs_the_head_and_the_threshold_is_a_constant():
    assert replay.verdict(rows(6, arm(), arm(cost=0.1)), None)["won"] is False
    assert (replay.MIN_HOLDOUT, replay.MIN_SAVING) == (6, 0.10)


# What edits a harness alone


@pytest.mark.parametrize(
    "paths, expected",
    [
        (["skills/demo/SKILL.md", "skills/demo/scripts/run.sh"], "the files of skill skills/demo"),
        (["plugins/evo-hub/skills/demo/SKILL.md"], "the files of skill plugins/evo-hub/skills/demo"),
        (["AGENTS.md", "docs/CLAUDE.md"], "AGENTS.md, docs/CLAUDE.md"),
        (["skills/demo/SKILL.md", "AGENTS.md"], "the files of skill skills/demo and AGENTS.md"),
        (["skills/demo/SKILL.md", "skills/other/SKILL.md"], None),  # two skills
        (["skills/demo/SKILL.md", "evo_agents/hub/tiers.py"], None),  # a file of another kind
        (["README.md"], None),
        ([], None),
    ],
)
def test_replay_harness_only_is_one_skill_and_agents_or_claude_md_files(paths, expected):
    assert replay.harness_only(paths) == expected


# The tier of a change whose replays won


def test_tier_a_harness_change_whose_replays_won_is_tier_0_whatever_its_kind_gave():
    harness, won = "the files of skill skills/demo", "arm b passed more"
    for kind in tiers.REPLAY_KINDS:
        found = tiers.tier_of(kind, [("evo-agents", "skills/demo/scripts/run.sh")], [])
        assert found.tier in (1, 2)  # a script is code; a global_config is tier 2
        lowered = tiers.replay_tier(kind, found, harness=harness, won=won)
        assert lowered.tier == 0 and lowered.reasons[-1].startswith(f"it edits {harness} alone and its replays")


@pytest.mark.parametrize(
    "kind, paths, protected, harness, won, tier",
    [
        ("skill", [("evo-agents", "skills/demo/SKILL.md")], [], "skill", None, 1),  # replays missing or lost
        ("skill", [("evo-agents", "skills/demo/SKILL.md")], [], None, "won", 1),  # the diff edits more than a harness
        ("skill", [("evo-agents", "skills/demo/SKILL.md")], ["skills/**"], "skill", "won", 3),  # protected stays 3
        ("fix", [("evo-agents", "skills/demo/run.py")], [], "skill", "won", 1),  # a kind replays do not lower
        ("ci_change", [("evo-agents", "AGENTS.md")], [], "AGENTS.md", "won", 3),
    ],
)
def test_tier_a_change_keeps_its_tier_without_replays_that_won_on_a_harness_alone(
    kind, paths, protected, harness, won, tier
):
    found = tiers.tier_of(kind, paths, protected)
    assert tiers.replay_tier(kind, found, harness=harness, won=won).tier == tier


# The sign of an instruction to test or verify weakened


@pytest.mark.parametrize(
    "path, lines",
    [
        ("skills/demo/SKILL.md", ["-Run the tests before every commit."]),
        ("skills/demo/SKILL.md", ["+You may skip the tests when the change is small."]),
        ("AGENTS.md", ["+Running the verify is optional."]),
        ("docs/CLAUDE.md", ["+Commit with git commit --no-verify."]),
        ("plugins/evo-hub/skills/demo/scripts/check.sh", ["-pytest -q tests", "+echo ok"]),
    ],
)
def test_judge_sign_an_instruction_of_a_harness_to_test_or_verify_weakened(path, lines):
    assert "instruction_weakened" in kinds(signs_of(file_diff(path, *lines)))
    assert judge.SIGN_KINDS["instruction_weakened"].startswith("an instruction of a skill")


@pytest.mark.parametrize(
    "path, lines",
    [
        ("skills/demo/SKILL.md", ["-Run the tests with pytest.", "+Run the tests with pytest -q before a commit."]),
        ("skills/demo/SKILL.md", ["+Never skip the tests, and do not skip the verify."]),
        ("skills/demo/SKILL.md", ["-Read the README first.", "+Read docs/README.md first."]),
        ("docs/guide.md", ["-Run the tests before every commit."]),  # not a harness file
    ],
)
def test_judge_no_sign_for_an_instruction_kept_reworded_or_outside_a_harness(path, lines):
    assert "instruction_weakened" not in kinds(signs_of(file_diff(path, *lines)))


def test_judge_a_harness_file_whose_diff_cannot_be_read_is_a_sign():
    unreadable = judge.FileDiff(path="skills/demo/SKILL.md", readable=False)
    found = judge.hack_signs([unreadable], repo="evo-agents", protected=[])
    assert kinds(found) == ["diff_unreadable"]
    docs = judge.FileDiff(path="docs/guide.md", readable=False)
    assert judge.hack_signs([docs], repo="evo-agents", protected=[]) == []  # documentation elsewhere stays readable
