"""Release runs on the worker, without Postgres: the daemon in this process with the fake runtime, the in-memory hub,
and the checkout alpha of ``tests.worker.test_plan_runs`` (``evo_agents.worker.runner.release``).

What step 2 of the plan daily-release names on the worker (criterion a2): the daemon says it runs release runs; a
release run's agent works in EVO_RUN_KIND release, one headless turn, in a worktree of the release's repo at the commit
its claim names, on a branch of the run; at its end the daemon commits what the agent left and pushes HEAD to
release/<version>, the version the agent wrote in .evo-run/result.json, never a default branch and never a tag, and
tells the hub of the push with the agent's Decision line; a release run pushes no other branch (``gitops.check_push``);
without a version it pushes nothing and fails; Claude Code gives it no question tool."""

from __future__ import annotations

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.hub import author, release_runs, runs
from evo_agents.worker import gitops
from evo_agents.worker.run import KINDS, ReleaseRun
from tests.worker.test_plan_runs import (
    WAIT,
    git,
    machine,  # noqa: F401 (a fixture)
    with_daemon,
)

DECISION = "Decision: 0.25.0, a minor: the range merges #7 from feat/release-runs."


def release_calls(hub) -> list[dict]:
    return [call for call in hub.chat_calls if call["route"] == "release/branch"]


def test_a_release_run_pushes_release_version_of_its_repo_and_tells_the_hub(machine):  # noqa: F811
    start = machine.seeds["alpha"]
    machine.scenarios(
        {
            "release": [
                {"sh": "echo $EVO_RUN_KIND; git -C alpha rev-parse HEAD; git -C alpha symbolic-ref --short HEAD"},
                {"write": {"alpha/VERSION": "0.25.0\n"}},
                {"sh": "git -C alpha add -A && git -C alpha commit -qm 'Release 0.25.0: the version'"},
                {"write": {"alpha/NOTES.md": "left for the daemon to commit\n"}},
                {"result": {"version": "0.25.0", "decision": DECISION, "summary": "Bumped; no fault in the range."}},
                {"say": "Cut 0.25.0."},
            ]
        }
    )

    async def body(hub, daemon):
        run_id = hub.queue_release_run("alpha", start)
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        assert hub.moves(run_id) == ["leased", "running", "verifying", "done"]
        assert hub.runs[run_id]["summary"] == "Bumped; no fault in the range."
        assert "release" in hub.last_heartbeat["run_kinds"]
        look = next(entry for entry in machine.commands() if entry["run"] == run_id)
        kind, at, local = look["stdout"].split()
        assert (kind, at, local) == ("release", start, f"evo-run/{run_id}/alpha")
        head = machine.origin_rev("alpha", "refs/heads/release/0.25.0")
        assert head is not None, "the daemon pushed release/<version>"
        log = machine.origin("alpha", "log", "--format=%s", "release/0.25.0").splitlines()
        assert log[0].startswith(f"run #{run_id}: ") and log[1] == "Release 0.25.0: the version"
        assert machine.origin("alpha", "show", "release/0.25.0:VERSION") == "0.25.0"
        assert machine.origin_rev("alpha", "refs/heads/main") == start, "never a default branch"
        assert machine.origin("alpha", "tag", "--list") == "", "never a tag"
        (told,) = release_calls(hub)
        assert told == {
            "run_id": run_id,
            "route": "release/branch",
            "repo": "alpha",
            "branch": "release/0.25.0",
            "head": head,
            "version": "0.25.0",
            "decision": DECISION,
        }
        texts = hub.texts(run_id)
        assert any(f"The agent picked 0.25.0: {DECISION}" in text for text in texts)
        assert any("The hub took release/0.25.0 of alpha" in text for text in texts)
        assert len(machine.starts()) == 1, "one turn"

    with_daemon(machine, body)


def test_a_release_run_without_a_version_pushes_nothing_and_fails(machine):  # noqa: F811
    machine.scenarios(
        {
            "release": [
                {"sh": "echo 1 > alpha/VERSION && git -C alpha add -A && git -C alpha commit -qm 'Release'"},
                {"result": {"version": "next", "summary": "Bumped."}},
            ]
        }
    )

    async def body(hub, daemon):
        run_id = hub.queue_release_run("alpha", machine.seeds["alpha"])
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "failed"
        assert "no version X.Y.Z" in (hub.runs[run_id].get("error") or ""), hub.texts(run_id)
        assert release_calls(hub) == []
        assert git("ls-remote", "--heads", str(machine.origins["alpha"])).count("refs/heads/") == 1, "main alone"

    with_daemon(machine, body)


def test_a_release_run_that_commits_nothing_fails_without_a_push(machine):  # noqa: F811
    machine.scenarios({"release": [{"result": {"version": "0.25.0", "decision": DECISION, "summary": "Nothing."}}]})

    async def body(hub, daemon):
        run_id = hub.queue_release_run("alpha", machine.seeds["alpha"])
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "failed"
        assert "committed nothing" in (hub.runs[run_id].get("error") or ""), hub.texts(run_id)
        assert machine.origin_rev("alpha", "refs/heads/release/0.25.0") is None and release_calls(hub) == []

    with_daemon(machine, body)


def test_check_push_lets_a_release_run_push_release_version_alone():
    assert gitops.check_push("release/0.25.0", ("main",), kind="release", plan_branch=None) is False
    for branch, why in (
        ("main", "a default branch: a release run never pushes it"),
        ("trunk", "a default branch"),
        ("release/next", "not a branch of a release: a release run pushes release/<version> alone"),
        ("release/0.25", "not a branch of a release"),
        ("release/v0.25.0", "not a branch of a release"),
        ("task/41", "not a branch of a release"),
        ("v0.25.0", "not a branch of a release"),
    ):
        with pytest.raises(gitops.PushRefused, match=why):
            gitops.check_push(branch, ("trunk",), kind="release", plan_branch=branch)
    assert release_runs.is_release_branch("release/0.25.0") and not release_runs.is_release_branch("release/0.25.00")
    assert KINDS["release"] is ReleaseRun and "release" in runs.RUN_KINDS


def test_claude_code_gives_a_release_run_no_question_tool(tmp_path):
    from tests.worker.test_budget import claude, command_of
    from tests.worker.test_runtimes import _samples

    release = claude(tmp_path, _samples, kind="release")
    release.options = release.build_options("/opt/bin/claude")
    assert release.options.disallowed_tools == list(author.QUESTION_TOOLS)
    assert "--disallowedTools" in command_of(release)
