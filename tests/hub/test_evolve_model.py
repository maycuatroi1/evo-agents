"""The Pareto operators, the merge, the light tasks and the baseline cache key of ``evo_agents.hub.evolve``."""

import random

import pytest

from evo_agents.hub import evolve

# Three candidates on three tasks: s0 the seed, a specialist of t3 alone, b best in total.
SCORES = {
    "s0": {"t1": 1, "t2": 0, "t3": 0},
    "a": {"t1": 0, "t2": 0, "t3": 1},
    "b": {"t1": 1, "t2": 1, "t3": 0},
    "c": {"t1": 1, "t2": 1, "t3": 0},
}


def test_pruning_keeps_every_tasks_ceiling_and_the_unique_specialist():
    survivors = evolve.prune_all(SCORES)
    top = evolve.ceiling(SCORES)
    assert evolve.ceiling({cand: SCORES[cand] for cand in survivors}) == top
    assert "a" in survivors  # alone at the ceiling of t3, though its total is the lowest
    assert "s0" not in survivors  # every task it tops, b tops too
    assert len({"b", "c"} & set(survivors)) == 1  # of two equal candidates, one is enough


def test_of_two_equal_candidates_the_cheaper_one_survives():
    costs = {"b": {"cost_usd": 2.0, "turns": 10}, "c": {"cost_usd": 0.5, "turns": 10}}
    assert "c" in evolve.prune_all(SCORES, costs) and "b" not in evolve.prune_all(SCORES, costs)
    costs = {"b": {"cost_usd": 1.0, "turns": 3}, "c": {"cost_usd": 1.0, "turns": 9}}
    assert evolve.prune_all(SCORES, costs) == ["a", "b"]


def test_pruning_any_pool_keeps_its_ceiling():
    rng = random.Random(7)
    for _ in range(200):
        pool = {f"p{i}": {f"t{j}": rng.randint(0, 3) for j in range(5)} for i in range(rng.randint(1, 8))}
        survivors = evolve.prune_all(pool)
        assert survivors and evolve.ceiling({c: pool[c] for c in survivors}) == evolve.ceiling(pool)
        for task, front in evolve.fronts(pool).items():
            if len(front) == 1:
                assert front <= set(survivors), task


def test_parents_are_drawn_by_how_often_they_are_on_the_front():
    survivors = evolve.prune_all(SCORES)
    freq = evolve.front_freq(SCORES, survivors)
    assert freq["a"] == 1 and max(freq.values()) == 2
    drawn = {evolve.pick_parent(freq, random.Random(seed)) for seed in range(50)}
    assert drawn == set(survivors)
    assert evolve.pick_parent({"x": 0, "y": 0}) == "x"
    with pytest.raises(evolve.EvolveProblem):
        evolve.pick_parent({})


def test_a_merge_takes_what_each_side_changed_and_leaves_conflicts_to_the_agent():
    parents = {"b": ["s0"], "c": ["b"], "d": ["s0"]}
    assert evolve.lca(parents, "c", "d") == "s0" and evolve.lca(parents, "c", "b") == "b"
    assert evolve.lca({}, "x", "y") is None
    big = "x" * 300_000  # no length is capped
    ancestor = {"skills/a/SKILL.md": "base", "AGENTS.md": "agents", "CLAUDE.md": "claude"}
    ours = {**ancestor, "skills/a/SKILL.md": big, "CLAUDE.md": "ours"}
    theirs = {**ancestor, "AGENTS.md": "new agents", "CLAUDE.md": "theirs", "skills/a/ref.md": "added"}
    merged = evolve.lca_merge(ancestor, ours, theirs)
    assert merged.components["skills/a/SKILL.md"] == big and merged.components["AGENTS.md"] == "new agents"
    assert merged.components["skills/a/ref.md"] == "added" and "CLAUDE.md" not in merged.components
    assert [item.key for item in merged.conflicts] == ["CLAUDE.md"] and merged.useful
    request = evolve.synthesis_request(merged)
    assert request["conflicts"][0] == {"key": "CLAUDE.md", "ancestor": "claude", "ours": "ours", "theirs": "theirs"}
    done = evolve.apply_synthesis(merged, {"CLAUDE.md": "both"})
    assert done["CLAUDE.md"] == "both" and done["skills/a/SKILL.md"] == big
    with pytest.raises(evolve.EvolveProblem, match="no answer"):
        evolve.apply_synthesis(merged, {})


def test_a_merge_of_orthogonal_edits_inherits_both_and_a_deletion_stays_deleted():
    ancestor = {"instructions": "i", "skill": "s"}
    merged = evolve.lca_merge(ancestor, {"instructions": "i2", "skill": "s"}, {"instructions": "i"})
    assert merged.components == {"instructions": "i2"} and not merged.conflicts and merged.useful
    same = evolve.lca_merge(ancestor, {"instructions": "i2", "skill": "s"}, ancestor)
    assert not same.useful


def test_light_tasks_read_the_select_split_alone_through_the_commit_map():
    old, new = "a" * 40, "b" * 40
    gone = "c" * 40
    commit_map = evolve.parse_commit_map(f"old new\n{old} {new}\n{gone} {'0' * 40}\n")
    tasks = [
        {"id": 1, "split": "holdout", "repo": "r", "commits": [old], "files": 1},
        {"id": 2, "split": "select", "repo": "r", "commits": [old[:9]], "files": 3, "base_sha": old},
        {"id": 3, "split": "select", "repo": "r", "commits": ["d" * 40], "files": 1},
        {"id": 4, "split": "select", "repo": "r", "commits": [gone], "files": 1},
        {"id": 5, "split": "select", "repo": "r", "commits": ["e" * 40], "files": 50},
        {"id": 6, "split": "select", "repo": "r", "commits": ["f" * 40], "duration_ms": 10**9},
        {"id": 7, "split": "select", "repo": "other", "commits": ["f" * 40]},
    ]
    probes = [
        {"id": 10, "repo": "r", "state": "active", "paths": ["skills/a/SKILL.md"]},
        {"id": 11, "repo": "r", "state": "retired"},
        {"id": 12, "repo": "r", "state": "active", "arms": {"on": {"skills/a/ref.md": "x"}, "off": {}}},
        {"id": 13, "repo": "r", "state": "active", "paths": ["AGENTS.md"]},
    ]
    found = evolve.light_tasks(tasks, probes, repo="r", paths=["skills/a/SKILL.md"], commit_map=commit_map)
    assert [(item["kind"], item["id"]) for item in found] == [
        ("replay", 3),
        ("replay", 2),
        ("probe", 10),
        ("probe", 12),
    ]
    assert found[1]["commits"] == [new] and found[1]["base_sha"] == new


def test_light_tasks_never_read_a_holdout_task_past_its_split():
    class Sealed(dict):
        def get(self, key, default=None):
            assert key == "split", f"read {key} of a holdout task"
            return super().get(key, default)

        def __getitem__(self, key):
            raise AssertionError(f"read {key} of a holdout task")

    assert evolve.light_tasks([Sealed(split="holdout")]) == []


def test_the_baseline_arms_key_names_every_part_and_caps_no_length():
    digest = evolve.baseline_digest({"AGENTS.md": "y" * 1_000_000, "gone.md": None})
    assert digest != evolve.baseline_digest({"AGENTS.md": "y" * 1_000_001, "gone.md": None})
    key = evolve.arm_key("replay", 4, "a" * 40, digest, "claude-code", "m")
    assert key == evolve.arm_key("replay", 4, "a" * 40, digest, "claude-code", "m")
    for other in [
        ("probe", 4, "a" * 40, digest, "claude-code", "m"),
        ("replay", 5, "a" * 40, digest, "claude-code", "m"),
        ("replay", 4, "b" * 40, digest, "claude-code", "m"),
        ("replay", 4, "a" * 40, "0" * 64, "claude-code", "m"),
        ("replay", 4, "a" * 40, digest, "codex", "m"),
        ("replay", 4, "a" * 40, digest, "claude-code", None),
    ]:
        assert evolve.arm_key(*other) != key
    with pytest.raises(evolve.EvolveProblem):
        evolve.arm_key("holdout", 4, None, digest, "claude-code", None)


def test_evolvable_paths_are_harness_files_the_charter_names():
    globs = ["skills/**", "r:AGENTS.md"]
    assert evolve.evolvable("r", "skills/a/SKILL.md", globs) and evolve.evolvable("r", "AGENTS.md", globs)
    assert not evolve.evolvable("q", "AGENTS.md", globs) and not evolve.evolvable("r", "skills/a.py", ["**"])


def test_asi_of_bounds_each_entry_and_show_prints_a_line_of_each_failed_task(capsys):
    from evo_agents.hub.cli.curator import evolve as evolve_cli

    traces = {
        "replay:1": {
            "checks": [{"exit_code": 1, "output_tail": "y" * 5000 + "\nfatal: no such repo\n"}],
            "n": [1] * 30,
        },
        "replay:2": {"checks": [{"exit_code": 0}]},
        "probe:3": "no object",
    }
    found = evolve.asi_of(traces, ["replay:1", "replay:2"])
    assert set(found) == {"replay:1", "replay:2"}
    assert len(found["replay:1"]["checks"][0]["output_tail"]) == evolve.ASI_TEXT and len(found["replay:1"]["n"]) == 20
    assert evolve.asi_of([]) == {} and evolve.asi_of(None) == {}
    candidates = [
        {"id": 4, "scores": {"replay:1": 0.0, "replay:2": 1.0}, "asi": found},
        {
            "id": 5,
            "scores": {"replay:1": 0.0},
            "asi": {"replay:1": {"checks": [], "agent_error": "the turn timed out"}},
        },
    ]
    evolve_cli._print_asi(candidates)
    assert capsys.readouterr().out.splitlines() == [
        "#4 replay:1: exit 1: fatal: no such repo",
        "#5 replay:1: the turn timed out",
    ]
