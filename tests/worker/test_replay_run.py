"""A replay run on the worker (``evo_agents.worker.runner.replay``), without Postgres: the daemon in this process with
the fake runtime, the in-memory hub, and the checkouts alpha and beta of ``tests.worker.test_plan_runs``.

The checks step 10 of the history-driven-curator plan names on the worker: each arm in a new repository at the task's
base commit that holds nothing of the history after it, arm a with the harness files of the default branch and arm b
with those of the change's branch (a skill whole under .claude/skills of the arm's directory), the step's verify run on
each arm's work as code the worker does not trust (no worker variable, no credential), each arm's result posted with the
run's own key, half the run's caps for each arm, nothing pushed; and a task whose commits are not on origin refused as
unusable without an agent."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from tests.worker.test_plan_runs import (
    git,
    machine,  # noqa: F401 (a fixture)
    with_daemon,
)

BRANCH = "curator/1-helper"
VERIFY = "env > {env}; test -f done.txt && grep -q done done.txt"
BUDGET = {"max_usd": 1.0, "max_turns": 30, "max_seconds": 1200, "spent_usd": 0.0, "spent_seconds": 0.0}


def commit(checkout, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        target = checkout / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git("add", "-A", cwd=checkout)
    git("commit", "--quiet", "-m", message, cwd=checkout)
    return git("rev-parse", "HEAD", cwd=checkout)


def history(machine) -> dict:  # noqa: F811
    """On alpha's main: the harness at v1 (the step's base), the step's commit, the harness at v2 and a later file;
    then the change's branch with the harness at v3. Every commit names."""
    alpha = machine.checkouts["alpha"]
    found = {"seed": machine.seeds["alpha"]}
    harness = {"AGENTS.md": "rules v1\n", "skills/helper/SKILL.md": "skill v1\n", "skills/helper/notes.md": "notes\n"}
    found["base"] = commit(alpha, harness, "the harness at v1")
    found["step"] = commit(alpha, {"done.txt": "done\n"}, "the step of the plan")
    found["later"] = commit(alpha, {"AGENTS.md": "rules v2\n", "future.txt": "later\n"}, "after the step")
    git("push", "--quiet", "origin", "main", cwd=alpha)
    git("checkout", "--quiet", "-b", BRANCH, cwd=alpha)
    found["head"] = commit(alpha, {"AGENTS.md": "rules v3\n", "skills/helper/SKILL.md": "skill v3\n"}, "the change")
    git("push", "--quiet", "origin", BRANCH, cwd=alpha)
    git("checkout", "--quiet", "main", cwd=alpha)
    return found


def curator(machine, commits: list[str], *, repo: str = "alpha", head: str | None = None, base: str | None = None):  # noqa: F811
    task = {
        "id": 7,
        "plan_id": "fleet",
        "step_key": "3",
        "repo": "alpha",
        "title": "Write done.txt",
        "what": "Write done.txt in alpha with the word done.",
        "verify": [VERIFY.format(env=machine.tmp / "verify-env.txt")],
        "commits": commits,
        "base_sha": base,
    }
    return {
        "change_id": 1,
        "branch": BRANCH,
        "forge": "github",
        "base_branch": "main",
        "head_sha": head,
        "protected_paths": ["AGENTS.md"],
        "replay": {"replay_id": 3, "task": task, "repo": repo},
    }


def probe(arm: str, machine) -> dict:  # noqa: F811
    """An action that writes what the arm's agent sees to files of the test."""
    out = machine.tmp / f"seen-{arm}"
    return {
        "sh": (
            f"mkdir -p {out}; cat alpha/AGENTS.md > {out}/agents; git -C alpha log --format=%H > {out}/log; "
            f"cat .claude/skills/helper/SKILL.md > {out}/skill; ls .claude/skills/helper > {out}/skill-files; "
            f"pwd > {out}/cwd"
        )
    }


def seen(machine, arm: str, name: str) -> str:  # noqa: F811
    return (machine.tmp / f"seen-{arm}" / name).read_text(encoding="utf-8")


def test_replay_run_runs_both_arms_from_the_base_commit_and_posts_each_with_its_key(machine):  # noqa: F811
    commits = history(machine)
    machine.scenarios(
        {
            "replay": [probe("a", machine), {"say": "I looked and wrote nothing."}],
            "replay/2": [probe("b", machine), {"write": {"alpha/done.txt": "done\n"}}, {"say": "Wrote done.txt."}],
        }
    )
    found = {}

    async def body(hub, daemon):
        spec = curator(machine, [commits["step"][:9]], head=commits["head"])
        run_id = hub.queue_replay_run([{"repo": "alpha", "branch": BRANCH}], spec, budget=BUDGET)
        assert await hub.wait_state(run_id, "done", "failed", timeout=60) == "done", hub.texts(run_id)
        found.update(run=run_id, hub=hub)

    with_daemon(machine, body)
    run_id, hub = found["run"], found["hub"]
    assert hub.moves(run_id) == ["leased", "running", "verifying", "done"]
    first, second = hub.replays
    assert [first["arm"], second["arm"]] == ["a", "b"]
    assert first["base_sha"] == second["base_sha"] == commits["base"]
    a, b = first["result"], second["result"]
    assert (a["passed"], b["passed"]) == (False, True)
    assert (a["harness_sha"], b["harness_sha"]) == (commits["later"], commits["head"])
    assert a["harness_paths"] == b["harness_paths"] == ["AGENTS.md", "skills/helper/SKILL.md"]
    assert [item["exit_code"] for item in a["verify"]] != [0] and [item["exit_code"] for item in b["verify"]] == [0]
    assert a["duration_ms"] >= 0 and a["tool_errors"] == 0 and a["session_id"]
    # each arm in a repository of its own at the base commit, with the harness of its side and no later history
    assert (seen(machine, "a", "agents"), seen(machine, "b", "agents")) == ("rules v2\n", "rules v3\n")
    assert (seen(machine, "a", "skill"), seen(machine, "b", "skill")) == ("skill v1\n", "skill v3\n")
    assert seen(machine, "a", "skill-files").split() == ["SKILL.md", "notes.md"]
    for arm in ("a", "b"):
        log = seen(machine, arm, "log").split()
        assert log[1:] == [commits["base"], commits["seed"]], log  # the harness commit on top of the base, no more
        assert commits["step"] not in log and commits["later"] not in log
        assert seen(machine, arm, "cwd").strip().endswith(f"/{arm}")
    # the verify ran as code the worker does not trust: none of the worker's variables
    env = (machine.tmp / "verify-env.txt").read_text(encoding="utf-8")
    assert "EVO_RUN_ID" not in env and "EVO_WORKER_HOME" not in env
    # half the run's caps for each arm's agent
    starts = [item for item in machine.starts() if item["run"] == run_id]
    assert [start["cwd"].rsplit("/", 1)[-1] for start in starts] == ["a", "b"]
    assert [start["budget"]["max_usd"] for start in starts] == [0.5, 0.5]
    assert starts[0]["budget"]["max_seconds"] == 600 and starts[1]["budget"]["max_seconds"] >= 600
    assert all("Write done.txt in alpha with the word done." in start["prompt"] for start in starts)
    assert machine.origin_rev("alpha", f"refs/heads/{BRANCH}") == commits["head"]  # nothing pushed
    assert machine.origin_rev("alpha", "refs/heads/main") == commits["later"]
    assert hub.judge_refused == []


def test_replay_run_of_a_change_in_another_repo_writes_its_files_in_a_folder_of_their_own(machine):  # noqa: F811
    commits = history(machine)
    beta = machine.checkouts["beta"]
    commit(beta, {"CLAUDE.md": "beta v1\n"}, "beta's harness")
    git("push", "--quiet", "origin", "main", cwd=beta)
    git("checkout", "--quiet", "-b", BRANCH, cwd=beta)
    beta_head = commit(beta, {"CLAUDE.md": "beta v2\n"}, "the change of beta")
    git("push", "--quiet", "origin", BRANCH, cwd=beta)
    out = machine.tmp / "seen"
    look = {"sh": f"mkdir -p {out}; cat beta/CLAUDE.md >> {out}/claude; ls beta >> {out}/files; ls -a alpha >> {out}/a"}
    machine.scenarios({"replay": [look], "replay/2": [look]})
    found = {}

    async def body(hub, daemon):
        spec = curator(machine, [commits["step"]], repo="beta", head=beta_head)
        repos = [{"repo": "alpha", "branch": None}, {"repo": "beta", "branch": BRANCH}]
        run_id = hub.queue_replay_run(repos, spec)
        assert await hub.wait_state(run_id, "done", "failed", timeout=60) == "done", hub.texts(run_id)
        found.update(hub=hub)

    with_daemon(machine, body)
    first, second = found["hub"].replays
    assert first["result"]["harness_paths"] == ["CLAUDE.md"] and second["result"]["harness_sha"] == beta_head
    assert (out / "claude").read_text(encoding="utf-8") == "beta v1\nbeta v2\n"
    assert (out / "files").read_text(encoding="utf-8").split() == ["CLAUDE.md", "CLAUDE.md"]  # its files alone
    assert "README.md" in (out / "a").read_text(encoding="utf-8")


def test_replay_run_of_a_task_whose_commits_are_not_on_origin_refuses_it_without_an_agent(machine):  # noqa: F811
    history(machine)
    machine.scenarios({"replay": [{"say": "never"}]})
    found = {}

    async def body(hub, daemon):
        run_id = hub.queue_replay_run([{"repo": "alpha", "branch": BRANCH}], curator(machine, ["abc1234def"]))
        assert await hub.wait_state(run_id, "done", "failed", timeout=60) == "done", hub.texts(run_id)
        found.update(hub=hub, run=run_id)

    with_daemon(machine, body)
    hub, run_id = found["hub"], found["run"]
    (sent,) = hub.replays
    assert sent["task_unusable"] is True and "none of the step's commits (abc1234def)" in sent["refused"]
    starts = machine.starts() if machine.starts_path.exists() else []
    assert [item for item in starts if item["run"] == run_id] == []
    assert hub.runs[run_id]["summary"].startswith("Nothing replayed: none of the step's commits")


def test_replay_runs_agent_command_of_a_plan_says_what_a_replay_run_does_instead(machine):  # noqa: F811
    commits = history(machine)
    machine.scenarios({"replay": [{"cli": ["step", "3", "in_progress"]}], "replay/2": []})
    found = {}

    async def body(hub, daemon):
        run_id = hub.queue_replay_run([{"repo": "alpha", "branch": BRANCH}], curator(machine, [commits["step"]]))
        assert await hub.wait_state(run_id, "done", "failed", timeout=60) == "done", hub.texts(run_id)
        found.update(run=run_id)

    with_daemon(machine, body)
    (command,) = [item for item in machine.commands() if item["run"] == found["run"]]
    assert command["exit"] != 0
    assert "is a replay run, which does a step of the project's history again" in command["stderr"]
    assert json.dumps(command)  # it ran in the arm's directory as the agent
