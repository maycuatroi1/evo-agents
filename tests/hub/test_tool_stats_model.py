"""How the hub adds up the tool calls of a run (``evo_agents.hub.server.tool_stats.summarize``), without Postgres: the
name each runtime's calls go under, what counts as a failure, and the time from a call to its result."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from evo_agents.hub.server.tool_stats import ToolFigures, summarize, tool_name

START = datetime(2026, 10, 8, 1, 0, tzinfo=UTC)


def event(second: float, kind: str, call: str | None, status: str | None, title=None, tool_kind=None):
    at = START + timedelta(seconds=second)
    return SimpleNamespace(at=at, kind=kind, call_id=call, title=title, tool_kind=tool_kind, status=status)


@pytest.mark.parametrize(
    "runtime, title, kind, expected",
    [
        ("claude-code", "Bash", "execute", "Bash"),
        ("claude-code", "mcp__plugin_evo-hub_evo-hub__plan_step", "other", "mcp__plugin_evo-hub_evo-hub__plan_step"),
        ("codex", "/bin/zsh -lc \"printf 'hi' > codex.txt\"", "execute", "execute"),
        ("codex", "edit hello.txt", "edit", "edit"),
        ("codex", "evo-hub/plan_step", "other", "evo-hub/plan_step"),
        ("opencode", "hello.txt", "edit", "edit"),
        ("opencode", "custom tool", "other", "custom tool"),
        ("claude-code", "", "read", "read"),
        ("codex", None, None, "unknown"),
        ("claude-code", "x" * 500, "other", "x" * 200),
    ],
)
def test_tool_stats_name_each_runtime_s_calls(runtime, title, kind, expected):
    assert tool_name(runtime, title, kind) == expected


def test_tool_stats_count_calls_failures_and_the_time_to_each_result():
    events = [
        event(0, "tool_call", "c1", "pending", "Bash", "execute"),
        event(1.5, "tool_call_update", "c1", "completed"),
        event(2, "tool_call", "c2", "pending", "Read", "read"),
        event(2.2, "tool_call_update", "c2", "failed"),
        event(3, "tool_call", "c3", "pending", "Bash", "execute"),
        event(3.1, "tool_call_update", "c3", "in_progress"),
        event(3.5, "tool_call_update", "c3", "failed"),
        event(9, "tool_call_update", "c3", "completed"),  # a later update moves neither the time nor the failure
        event(4, "tool_call", "c4", "pending", "Edit", "edit"),  # never finished: a call, no time
        event(5, "tool_call", "c1", "pending", "Bash", "execute"),  # the same call again counts once
        event(6, "tool_call_update", "lost", "failed"),  # an update of a call whose start is not in the log
        event(7, "tool_call", None, "pending", "Bash", "execute"),  # no call id
    ]
    assert summarize("claude-code", events) == [
        ToolFigures("Bash", calls=2, errors=1, duration_ms=2000),
        ToolFigures("Edit", calls=1, errors=0, duration_ms=0),
        ToolFigures("Read", calls=1, errors=1, duration_ms=200),
    ]
    assert summarize("claude-code", []) == []


def test_tool_stats_of_codex_go_under_the_call_kind():
    events = [
        event(0, "tool_call", "exec-1", "in_progress", "pytest -q", "execute"),
        event(4, "tool_call_update", "exec-1", "completed"),
        event(5, "tool_call", "exec-2", "in_progress", "ruff check .", "execute"),
        event(5.5, "tool_call_update", "exec-2", "failed"),
    ]
    assert summarize("codex", events) == [ToolFigures("execute", calls=2, errors=1, duration_ms=4500)]
