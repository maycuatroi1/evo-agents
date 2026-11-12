"""Command runs on the worker, without Postgres: the daemon in this process, the in-memory hub, and the two checkouts
(alpha and beta) of ``tests.worker.test_plan_runs`` (``evo_agents.worker.runner.command``).

What criterion a5 of the plan hub-cron-jobs names on the worker: the daemon says it runs command runs; a command run's
line runs with the machine's shell, in a worktree of its repo at the default branch or, with no repo, in the run's
empty directory, with the run's leases and no model's key; its stdout and stderr go to the run's log; exit 0 is done,
any other code failed with cause command_failed; past its timeout the command is killed and the run fails; no agent
starts and nothing is committed or pushed."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.hub import command_runs, runs
from evo_agents.hub.account_providers import MODEL_ENV_VARS
from evo_agents.worker.run import KINDS, CommandRun
from evo_agents.worker.runner import command as command_module
from tests.worker.test_credentials import env_lease
from tests.worker.test_plan_runs import (
    WAIT,
    git,
    machine,  # noqa: F401 (a fixture)
    with_daemon,
)


def output(hub, run_id: int, stream: str | None = None) -> list[str]:
    """The lines of output the run's log holds, of ``stream`` alone when named."""
    return [
        event["body"]["line"]
        for event in hub.runs[run_id]["events"]
        if event["kind"] == "output" and (stream is None or event["body"].get("stream") == stream)
    ]


def test_command_is_a_run_kind_of_the_hub_and_of_the_worker_and_no_night_shift_kind():
    assert "command" in runs.RUN_KINDS and KINDS["command"] is CommandRun
    assert "command" not in runs.CURATOR_KINDS
    assert command_runs.FAILURE_CAUSE in runs.FAILURE_CAUSES
    assert command_runs.DEFAULT_TIMEOUT_MINUTES == 30 and command_runs.MAX_TIMEOUT_MINUTES == 240
    assert command_runs.timeout_problem(241) is not None and command_runs.timeout_problem(240) is None
    assert command_runs.command_problem("  ") == "the command is empty"


def test_a_command_run_runs_in_a_worktree_of_its_repo_at_the_default_branch_and_ends_done(machine):  # noqa: F811
    assert "ANTHROPIC_API_KEY" in MODEL_ENV_VARS
    machine.env["ANTHROPIC_API_KEY"] = "test-model-key"  # the daemon's own: the command gets none of it
    line = (
        "pwd; git rev-parse HEAD; echo $EVO_RUN_KIND; echo ${#DEPLOY_TOKEN}; echo ${ANTHROPIC_API_KEY-unset}; "
        "echo warned >&2; echo made > made.txt"
    )

    async def body(hub, daemon):
        assert "command" in hub.last_heartbeat["run_kinds"]
        run_id = hub.queue_command_run(line, "alpha")
        hub.leases[run_id] = {"leases": [env_lease(1, "DEPLOY_TOKEN", "deploy-value-1234")], "missing": []}
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        assert hub.moves(run_id) == ["leased", "running", "verifying", "done"]
        worktree = machine.directory(run_id) / "alpha"
        assert output(hub, run_id, "stdout") == [str(worktree), machine.seeds["alpha"], "command", "17", "unset"]
        assert output(hub, run_id, "stderr") == ["warned"]
        assert hub.runs[run_id]["summary"] == "The command exited 0, after 6 line(s) of output."
        assert not machine.starts_path.exists(), "no agent starts"
        assert (worktree / "made.txt").read_text(encoding="utf-8") == "made\n"
        assert git("rev-parse", "HEAD", cwd=worktree) == machine.seeds["alpha"], "nothing is committed"
        assert machine.origin("alpha", "for-each-ref", "--format=%(refname)") == "refs/heads/main", "nor pushed"
        assert machine.origin_rev("alpha", "refs/heads/main") == machine.seeds["alpha"]

    with_daemon(machine, body)


def test_a_command_run_with_no_repo_runs_in_the_runs_empty_directory(machine):  # noqa: F811
    async def body(hub, daemon):
        run_id = hub.queue_command_run("pwd; ls -A | wc -l")
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        lines = output(hub, run_id)
        assert lines[0] == str(machine.directory(run_id)) and lines[1].strip() == "0"

    with_daemon(machine, body)


def test_a_command_that_exits_other_than_0_fails_the_run(machine):  # noqa: F811
    async def body(hub, daemon):
        run_id = hub.queue_command_run("echo trying; echo broke >&2; exit 3", "beta")
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "failed", hub.texts(run_id)
        assert hub.runs[run_id]["failure_cause"] == "command_failed"
        assert "The command exited 3" in hub.runs[run_id]["error"]
        assert output(hub, run_id) == ["trying", "broke"] or sorted(output(hub, run_id)) == ["broke", "trying"]
        assert not machine.starts_path.exists()

    with_daemon(machine, body)


def test_a_command_past_its_timeout_is_killed_and_the_run_fails(machine, monkeypatch):  # noqa: F811
    marker = machine.tmp / "late.txt"
    started = CommandRun.execute

    async def soon(self, command, cwd):
        self.run.timeout_s = 1  # the run's timeout, in seconds: a minute is the least a spec names
        return await started(self, command, cwd)

    monkeypatch.setattr(CommandRun, "execute", soon)
    monkeypatch.setattr(command_module, "GRACE", 1.0)

    async def body(hub, daemon):
        run_id = hub.queue_command_run(f"echo started; sleep 30; touch {marker}; echo late")
        began = time.monotonic()
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "failed", hub.texts(run_id)
        assert time.monotonic() - began < 20, "the command was killed at its timeout, not waited for"
        assert hub.runs[run_id]["failure_cause"] == "timeout"
        assert output(hub, run_id) == ["started"]
        assert any("Command stopped (timeout)" in text for text in hub.texts(run_id))

    with_daemon(machine, body)
    assert not marker.exists(), "the shell's whole process group was killed"


def test_a_command_run_without_a_command_fails_before_anything_runs(machine):  # noqa: F811
    async def body(hub, daemon):
        run_id = hub.queue_command_run("echo never", "alpha")
        hub.runs[run_id]["spec"]["command"] = ""
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "failed", hub.texts(run_id)
        assert "the run holds no command to run" in hub.runs[run_id]["error"]
        assert output(hub, run_id) == []
        assert not machine.directory(run_id).exists()

    with_daemon(machine, body)
