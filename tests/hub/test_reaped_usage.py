"""The usage a run the reaper ends gets from its ``usage_update`` events (``evo_agents.hub.server.reaped_usage``), in
the shape its worker would have reported, for each runtime's events; and the copy migration 0183 keeps of it. No
database: tests/hub/test_runs.py runs the reaper, tests/hub/test_migrate.py the migration."""

import importlib

import pytest

from evo_agents.hub import runtime_metrics
from evo_agents.hub.server.reaped_usage import usage_of_events

MIGRATION = importlib.import_module("evo_agents.hub.migrations.versions.0183_reaped_run_usage")


def told(usage: dict | None = None, amount: float | None = None, currency: str = "USD", **extra) -> dict:
    """The body of a usage_update event, as ``runtimes.common.usage_update`` makes one."""
    body = {"usage": usage or {}, **extra}
    if amount is not None:
        body["cost"] = {"amount": amount, "currency": currency}
    return body


CLAUDE_CODE_SESSIONS = [  # two sessions of Claude Code: its running total falls back as the second begins
    told({"input_tokens": 10, "output_tokens": 100, "service_tier": "standard", "iterations": [{"x": 1}]}, 1.0),
    told({"input_tokens": 5, "output_tokens": 50}, 3.0, numTurns=4),
    told({"input_tokens": 2, "output_tokens": 20}, 0.5),
    told({"input_tokens": 1, "output_tokens": 10}, 2.0),
]
CASES = {
    "claude-code": (
        CLAUDE_CODE_SESSIONS,
        {"input_tokens": 18, "output_tokens": 180, "total_cost_usd": 5.0},
        5.0,
    ),
    "codex": (  # the thread's total, of the app-server (camelCase) and of a transcript the terminal UI wrote
        [
            told({"inputTokens": 100, "cachedInputTokens": 40, "outputTokens": 9}, last={"inputTokens": 100}),
            told({"inputTokens": 300, "cachedInputTokens": 200, "outputTokens": 30}),
            told({}),
        ],
        {"input_tokens": 300, "cached_input_tokens": 200, "output_tokens": 30},
        None,
    ),
    "opencode": (  # each step's tokens and cost
        [told({"input": 10, "output": 4}, 0.25, reason="tool-calls"), told({"input": 6, "output": 2}, 0.5)],
        {"input": 16, "output": 6, "cost": 0.75},
        0.75,
    ),
    "northwind/acp-agent": (  # an ACP agent: the session's cost so far, no tokens
        [told(None, 0.2, used=1000, size=200000), told(None, 0.9, used=4000, size=200000)],
        {"cost": 0.9},
        0.9,
    ),
}


@pytest.mark.parametrize("runtime", sorted(CASES))
def test_the_usage_of_events_takes_the_shape_each_runtime_reports(runtime):
    events, usage, cost = CASES[runtime]
    assert usage_of_events(runtime, events) == usage
    assert runtime_metrics.run_cost(usage) == cost
    assert MIGRATION._usage_of_events(runtime, events) == usage, "the migration's copy says the same"


def test_events_that_say_nothing_give_no_usage_and_a_cost_not_in_usd_is_left_out():
    assert usage_of_events("claude-code", []) is None
    assert usage_of_events("claude-code", [told(), "not a body", told({"service_tier": "standard"})]) is None
    assert usage_of_events("claude-code", [told(None, 3.0, currency="EUR"), told(None, -1.0)]) is None
    assert usage_of_events("lumen/acp", [told({"input_tokens": 7}, 2.0, currency="EUR")]) == {"input_tokens": 7}
    assert usage_of_events("claude-code", [told(None, True)]) is None, "a boolean is no amount"
