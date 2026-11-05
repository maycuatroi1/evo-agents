"""What a local session spent, read from Claude Code's transcript (``evo_agents.hub.local_usage``): each message's
tokens once, by model, the subagents' too, and Claude Code's cost-state lines as its own count; and what the hub's
ledger makes of them (``evo_agents.hub.run_spend``)."""

import json

import pytest

from evo_agents.hub import local_usage, run_spend

SESSION = "b2a7c4de-0000-4000-8000-000000000002"


def assistant(message_id: str, model: str, output: int, read: int = 0, hour: int = 0) -> dict:
    usage = {
        "input_tokens": 2,
        "output_tokens": output,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": hour,
        "cache_creation": {"ephemeral_1h_input_tokens": hour, "ephemeral_5m_input_tokens": 0},
        "output_tokens_details": {"thinking_tokens": output // 2},
    }
    return {"type": "assistant", "message": {"id": message_id, "model": model, "usage": usage, "content": []}}


def write(path, *lines: dict) -> None:
    with path.open("a") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")


def test_each_message_counts_once_across_hooks_and_a_cost_state_comes_as_claude_codes_count(tmp_path):
    transcript = tmp_path / f"{SESSION}.jsonl"
    opus, haiku = "claude-opus-5-5", "claude-haiku-5-5"
    write(transcript, assistant("m1", opus, 100, 1000, 50), assistant("m1", opus, 100, 1000, 50))  # two blocks
    write(transcript, {"type": "user", "message": {"content": "go"}}, assistant("s", "<synthetic>", 9))
    rec: dict = {}
    (first,) = local_usage.usage_events(rec, transcript, SESSION)
    assert first == (
        "usage_update",
        {
            "source": "local",
            "scope": "delta",
            "session_id": SESSION,
            "models": {
                opus: {
                    "inputTokens": 2,
                    "outputTokens": 100,
                    "cacheReadInputTokens": 1000,
                    "cacheCreationInputTokens": 50,
                    "cacheCreation1hInputTokens": 50,
                    "thinkingTokens": 50,
                }
            },
        },
    )
    write(transcript, assistant("m1", opus, 100, 1000, 50))  # the message's last block, read by the next hook
    assert local_usage.usage_events(rec, transcript, SESSION) == []
    subagents = tmp_path / SESSION / "subagents"
    subagents.mkdir(parents=True)
    write(subagents / "agent-a1.jsonl", assistant("h1", haiku, 10))
    state = {"type": "cost-state", "totalCostUSD": 0.9, "modelUsage": {opus: {"outputTokens": 300, "costUSD": 0.9}}}
    write(transcript, assistant("m2", opus, 200), state, assistant("m3", opus, 7))
    kinds = [(body["scope"], sorted(body["models"])) for _, body in local_usage.usage_events(rec, transcript, SESSION)]
    assert kinds == [("delta", [haiku, opus]), ("total", [opus]), ("delta", [opus])], "subagents before the session"
    assert local_usage.usage_events(rec, transcript, SESSION) == []


def test_the_ledger_prices_a_local_session_until_its_cost_state_and_takes_that_as_reported(tmp_path):
    transcript = tmp_path / f"{SESSION}.jsonl"
    write(transcript, assistant("m1", "claude-opus-5-5", 1000))
    rec: dict = {}
    first = local_usage.usage_events(rec, transcript, SESSION)
    (spent,) = run_spend.ledger(run_spend.reports_of("claude-code", first, "", ""))
    assert spent.reported_usd is None and spent.estimated_usd == pytest.approx((2 * 4 + 1000 * 20) / 1e6)
    counted = {"inputTokens": 2, "outputTokens": 1000, "thinkingTokens": 500, "costUSD": 0.03}
    state = {"type": "cost-state", "totalCostUSD": 0.03, "modelUsage": {"claude-opus-5-5": counted}}
    write(transcript, state)
    later = first + local_usage.usage_events(rec, transcript, SESSION)
    (spent,) = run_spend.ledger(run_spend.reports_of("claude-code", later, "", ""))
    assert spent.session == SESSION and spent.reported_usd == pytest.approx(0.03) and spent.estimated_usd is None
    assert spent.tokens == {"input": 2, "output": 1000, "reasoning": 500}, "the count holds the message before it"
