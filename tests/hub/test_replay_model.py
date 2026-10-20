"""The model of the Curator's replays (``evo_agents.hub.replay``) and the tasks the suite job makes of plans
(``evo_agents.hub.replay_suite.new_tasks``), without Postgres: which steps give a task, its fixed split, the files of a
harness, the base commit of a task, the prompt of a replay's agent, and what the arms of a change's replays add up to."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy", reason="the hub's replays needs the hub-server extra, evo-ak[hub-server]")

from evo_agents.hub import replay
from evo_agents.hub.replay_suite import new_tasks


def plan(*steps: dict, repos=None) -> dict:
    return {"id": "fleet", "repos": repos or [{"repo": "evo-agents", "branch": "main"}], "steps": list(steps)}


def step(key, **extra) -> dict:
    return {
        "id": key,
        "title": f"Step {key}",
        "what": f"Do step {key}.",
        "status": "done",
        "verify": f"python -m pytest -q tests/test_{key}.py",
        "evidence": f"Commits: 1a2b3c{key}d, 4e5f6a{key}b",
        **extra,
    }


SECRET = "session-secret-of-one-hub"


def test_replay_split_is_fixed_by_the_plan_and_step_and_holds_one_task_in_three_out():
    keys = [str(index) for index in range(300)]
    splits = [replay.split_of("fleet", key, SECRET) for key in keys]
    assert splits == [replay.split_of("fleet", key, SECRET) for key in keys]  # the same every time on a hub
    assert set(splits) == {"select", "holdout"}
    assert 70 < splits.count("holdout") < 130  # about one in three
    assert [replay.split_of("other", key, SECRET) for key in keys] != splits  # the plan counts, not the step alone


def test_replay_split_is_keyed_by_the_hubs_secret_so_the_code_alone_cannot_tell_the_holdout_tasks():
    keys = [str(index) for index in range(300)]
    splits = [replay.split_of("fleet", key, SECRET) for key in keys]
    assert [replay.split_of("fleet", key, "another hub's secret") for key in keys] != splits

    def public(key: str) -> str:  # the split of step 10, from the plan and step alone, which anyone can compute
        digest = hashlib.sha256(f"fleet#{key}".encode()).digest()
        return replay.SPLITS[1] if int.from_bytes(digest[:8], "big") % replay.HOLDOUT_EVERY == 0 else replay.SPLITS[0]

    assert [public(key) for key in keys] != splits
    agreed = sum(public(key) == split for key, split in zip(keys, splits, strict=True))
    assert agreed < 200  # no better than chance: two splits at random agree on five tasks in nine, about 167 of 300


@pytest.mark.parametrize(
    ("path", "harness", "skill"),
    [
        ("AGENTS.md", True, None),
        ("docs/CLAUDE.md", True, None),
        ("skills/curator/SKILL.md", True, "skills/curator"),
        ("plugins/hub/skills/using-hub/references/a.md", True, "plugins/hub/skills/using-hub"),
        ("skills/SKILL.md", False, None),
        ("README.md", False, None),
        ("evo_agents/hub/replay.py", False, None),
        ("../AGENTS.md", False, None),
    ],
)
def test_replay_harness_paths_are_agents_and_claude_files_and_the_files_of_a_skill(path, harness, skill):
    assert replay.harness_path(path) is harness
    assert replay.skill_dir(path) == skill


def test_replay_a_done_step_with_a_verify_and_commits_gives_a_task():
    body = plan(step(1))
    task = replay.task_of("fleet", body, 0, secret=SECRET)
    assert task == {
        "plan_id": "fleet",
        "step_key": "1",
        "repo": "evo-agents",
        "title": "Step 1",
        "what": "Do step 1.",
        "verify": "python -m pytest -q tests/test_1.py",
        "commits": ["1a2b3c1d", "4e5f6a1b"],
        "source": "evidence",
        "split": replay.split_of("fleet", "1", SECRET),
    }


@pytest.mark.parametrize(
    "changed",
    [
        {"status": "pending"},
        {"verify": None},
        {"verify": "pytest\nruff check ."},  # more than one line
        {"what": " "},
        {"evidence": "done, no commit named"},
        {"repo": None},  # and a plan of two repos
    ],
)
def test_replay_a_step_gives_no_task_without_all_it_needs(changed):
    body = plan(step(1, **changed), repos=[{"repo": "a"}, {"repo": "b"}])
    assert replay.task_of("fleet", body, 0, secret=SECRET) is None


def test_replay_a_step_of_a_curator_plan_gives_no_task_and_a_run_names_the_commits_evidence_does_not():
    assert replay.task_of("curator-3-fix", plan(step(1)), 0, secret=SECRET) is None
    body = plan(step(2, evidence="Done by the plan run."))
    task = replay.task_of("fleet", body, 0, ["0123abcdef", "0123abcdef", "not a sha"], secret=SECRET)
    assert (task["commits"], task["source"]) == (["0123abcdef"], "run")


def test_replay_suite_job_adds_the_tasks_it_does_not_have_with_the_plans_label():
    body = plan(step(1), step(2, evidence="no commit"), step(3, status="pending"), step(4))
    rows = [SimpleNamespace(plan_id="fleet", body=body, label={"level": "internal"})]
    commits = {("fleet", "2"): ["9f8e7d6c"]}
    found = new_tasks(rows, {("fleet", "4")}, commits, SECRET)
    assert [(task["step_key"], task["source"], task["label"]) for task in found] == [
        ("1", "evidence", {"level": "internal"}),
        ("2", "run", {"level": "internal"}),
    ]


def test_replay_base_commit_is_the_first_parent_of_the_oldest_commit_of_the_step():
    commits = [("c3", ["c2"], 300), ("c1", ["c0", "x"], 100), ("c2", ["c1"], 200)]
    assert replay.base_of(commits) == "c0"
    assert replay.base_of([("root", [], 5)]) is None
    assert replay.base_of([]) is None


def test_replay_prompt_says_the_rules_the_step_and_where_its_files_are():
    task = replay.task_of("fleet", plan(step(5)), 0, secret=SECRET)
    prompt = replay.replay_prompt("demo", task, folder="evo-agents", harness_folder="evo-agents-harness")
    assert "The repository evo-agents is in evo-agents/ under the current directory" in prompt
    assert "do not push" in prompt and "`evo-agents worker` commands do not work" in prompt
    assert "evo-agents-harness/ holds files of another repository" in prompt
    assert prompt.rstrip().endswith("Verify: python -m pytest -q tests/test_5.py")
    assert "# Step 5: Step 5\n\nDo step 5." in prompt


def test_replay_compare_adds_up_each_arm_and_counts_the_replays_with_both():
    a = {"passed": False, "cost_usd": 0.5, "turns": 10, "tool_errors": 2, "duration_ms": 1000, "peak_context_tokens": 9}
    b = {"passed": True, "cost_usd": 0.25, "turns": 4, "tool_errors": 0, "duration_ms": 500, "peak_context_tokens": 12}
    found = replay.compare([{"result_a": a, "result_b": b}, {"result_a": a, "result_b": None}])
    assert found["both"] == 1
    assert found["a"] == {
        "replays": 2,
        "passed": 0,
        "cost_usd": 1.0,
        "turns": 20.0,
        "tool_errors": 4.0,
        "duration_ms": 2000.0,
        "peak_context_tokens": 9.0,
    }
    assert (found["b"]["replays"], found["b"]["passed"], found["b"]["peak_context_tokens"]) == (1, 1, 12.0)
    empty = replay.compare([])
    assert empty["a"] == {
        **dict.fromkeys(("cost_usd", "turns", "tool_errors", "duration_ms", "peak_context_tokens")),
        "replays": 0,
        "passed": 0,
    }
