"""The auto-compact window of a run's agent on Claude Code (``claude_code.compact_window``, set in
``evo_agents.worker.runner.agent``): 200,000 tokens unless the daemon's environment sets Claude Code's own variable,
or EVO_WORKER_CLAUDE_CODE_AUTO_COMPACT_WINDOW sets another window or turns it off; the headless agent and the terminal
UI get the same; agents of other runtimes get nothing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from evo_agents.worker.adapter import RunContext
from evo_agents.worker.runner.agent import Agent
from evo_agents.worker.runtimes import claude_code
from evo_agents.worker.runtimes.claude_code import AUTO_COMPACT, AUTO_COMPACT_SETTING, compact_window


def test_the_window_is_200000_tokens_unless_the_worker_says_otherwise():
    assert claude_code.AUTO_COMPACT_WINDOW == 200_000
    assert compact_window({}) == {AUTO_COMPACT: "200000"}
    assert compact_window({AUTO_COMPACT_SETTING: "350000"}) == {AUTO_COMPACT: "350000"}
    assert compact_window({AUTO_COMPACT_SETTING: " 150000 "}) == {AUTO_COMPACT: "150000"}
    for off in ("off", "OFF", "0"):
        assert compact_window({AUTO_COMPACT_SETTING: off}) == {}, "Claude Code's own window"
    for wrong in ("500k", "lots", "-1"):
        assert compact_window({AUTO_COMPACT_SETTING: wrong}) == {AUTO_COMPACT: "200000"}
    own = {AUTO_COMPACT: "800000", AUTO_COMPACT_SETTING: "150000"}
    assert compact_window(own) == {}, "the daemon's own CLAUDE_CODE_AUTO_COMPACT_WINDOW stays as it is"


def run(tmp_path: Path, runtime: str, env: dict[str, str]) -> SimpleNamespace:
    """What Agent.env reads of a run."""
    return SimpleNamespace(
        id=7,
        runtime=runtime,
        project="demo",
        curator=None,
        spec={"kind": "step", "plan_id": "p", "step_key": "1"},
        daemon=SimpleNamespace(env={"PATH": "/usr/bin", **env}, home=SimpleNamespace(root=tmp_path / "worker")),
        credentials=SimpleNamespace(agent_vars={}, withheld=frozenset()),
    )


@pytest.mark.parametrize(
    "daemon_env, window",
    [({}, "200000"), ({AUTO_COMPACT_SETTING: "300000"}, "300000"), ({AUTO_COMPACT: "900000"}, "900000")],
)
def test_the_agent_of_a_claude_code_run_gets_the_window_headless_and_in_its_terminal_ui(tmp_path, daemon_env, window):
    from evo_agents.worker.interactive import ClaudeCodeTui

    env = Agent(run(tmp_path, "claude-code", daemon_env)).env()
    assert env[AUTO_COMPACT] == window and env["EVO_RUN_ID"] == "7"
    (tmp_path / "worktree").mkdir()
    context = RunContext(run={"id": 7}, worktree=tmp_path / "worktree", prompt="Do the step.", env=env)
    assert ClaudeCodeTui(context, None).environment()[AUTO_COMPACT] == window
    pytest.importorskip("claude_agent_sdk")
    options = claude_code.ClaudeCodeAdapter(context).build_options("/opt/bin/claude")
    assert options.env[AUTO_COMPACT] == window


def test_turned_off_or_on_another_runtime_the_agent_gets_no_window(tmp_path):
    assert AUTO_COMPACT not in Agent(run(tmp_path, "claude-code", {AUTO_COMPACT_SETTING: "off"})).env()
    for runtime in ("codex", "opencode"):
        assert AUTO_COMPACT not in Agent(run(tmp_path, runtime, {})).env()
