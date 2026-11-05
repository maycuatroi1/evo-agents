"""What a run spent, from its usage reports (``evo_agents.hub.run_spend``): each runtime's reports, the run's own share
of a session it went on in, the cost reported and the cost estimated at list prices. No database: the ledger's rows
and Run's ``spend`` are tests/hub/test_runs.py's."""

import pytest

from evo_agents.hub import run_spend
from evo_agents.hub.run_spend import Price, goes_on, ledger, price_of, reports_of, summary


def claude(tokens: tuple, cost: float | None, **extra) -> dict:
    """A usage_update of Claude Code: a turn's input, output, cache read and cache write (an hour's) tokens, and the
    session's cost so far."""
    given, output, read, written = tokens
    usage = {
        "input_tokens": given,
        "output_tokens": output,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": written,
        "cache_creation": {"ephemeral_1h_input_tokens": written, "ephemeral_5m_input_tokens": 0},
        "speed": "standard",
    }
    body = {"usage": usage, "numTurns": 3, **extra}
    if cost is not None:
        body["cost"] = {"amount": cost, "currency": "USD"}
    return body


# Run #1919 of evo-agents, a plan run with a session for each step on claude-opus-5-5: the turns' tokens and the cost
# each result said the session had cost so far. Each step's session starts from 0, so the total falls back or jumps
# to a new session's first turn; the last result goes on in the session of the one before.
RUN_1919 = [
    ((134, 46016, 6897334, 135378), 3.384220375),
    ((122, 74992, 5732037, 202867), 4.446161675),
    ((116, 37400, 5626869, 123073), 2.85942031),
    ((160, 70796, 6500827, 209651), 4.58135991),
    ((34, 7394, 780475, 45464), 0.66907971),
    ((48, 5920, 969537, 35851), 0.60040441),
    ((10, 1418, 249040, 2547), 0.69898841),
]
INIT = ("output", {"raw": {"type": "system", "subtype": "init", "model": "claude-opus-5-5", "tools": []}})


def events(*bodies) -> list[tuple[str, dict]]:
    return [("usage_update", body) for body in bodies]


def only(spent: list) -> run_spend.Spend:
    (item,) = spent
    return item


def test_the_list_price_of_opus_5_5_comes_to_the_cost_claude_code_reported_for_a_turn():
    tokens, cost = RUN_1919[0]
    priced = price_of("claude-opus-5-5").of(run_spend.claude_tokens(claude(tokens, None)["usage"]))
    assert priced == pytest.approx(cost, rel=1e-3)  # the rest is the turn's calls of a small model


@pytest.mark.parametrize(
    ("name", "price"),
    [
        ("claude-opus-5-5", Price(4, 20, 0.2)),
        ("claude-opus-5-5[1m]", Price(4, 20, 0.2)),
        ("Claude-Sonnet-4-6-20260101", Price(3, 15, 0.3)),
        ("anthropic/claude-haiku-4-5", Price(1, 5, 0.1)),
        ("us.anthropic.claude-fable-5-1", Price(10, 50, 0.25)),
        ("opus", None),
        ("gpt-5-codex", None),
        ("", None),
    ],
)
def test_a_price_is_found_by_the_model_without_its_provider_context_or_date(name, price):
    assert price_of(name) == price


def test_a_cache_write_for_an_hour_costs_twice_the_input_and_one_for_five_minutes_a_quarter_more():
    price = Price(4, 20, 0.2)
    assert price.of({"cache_write": 1_000_000}) == pytest.approx(5.0)
    assert price.of({"cache_write": 1_000_000, "cache_write_1h": 1_000_000}) == pytest.approx(8.0)
    assert price.of({"input": 1_000_000, "output": 1_000_000, "cache_read": 1_000_000}) == pytest.approx(24.2)


@pytest.mark.parametrize(
    ("before", "now", "expected", "on"),
    [
        (None, 3.0, 1.0, True),  # nothing before: both read the same
        (3.0, 2.0, None, False),  # it fell: a new process
        (3.0, 4.0, None, True),  # it grew and nothing tells otherwise
        (3.38, 4.45, 4.27, False),  # a new session whose first turn cost more than the one before had in all
        (0.60, 0.70, 0.10, True),  # the next turn of the same session
        (3.0, None, 1.0, True),  # no total: nothing to tell
    ],
)
def test_a_total_goes_on_when_its_step_is_nearer_to_what_its_turn_spent_than_the_total_itself(
    before, now, expected, on
):
    assert goes_on(before, now, expected) is on


def test_run_1919_spent_each_session_once_and_the_turn_of_its_last_session_that_went_on():
    reports = reports_of("claude-code", [INIT, *events(*(claude(t, c) for t, c in RUN_1919))], "s-1919", "opus")
    spent = only(ledger(reports))
    assert spent.model == "claude-opus-5-5", "the init message names the model, not the run's alias"
    own_turn = RUN_1919[-1][1] - RUN_1919[-2][1]
    assert spent.reported_usd == pytest.approx(sum(cost for _, cost in RUN_1919[:-1]) + own_turn)
    assert spent.estimated_usd is None, "every turn came with a cost"
    assert spent.tokens["output"] == sum(tokens[1] for tokens, _ in RUN_1919)
    assert spent.tokens["cache_write_1h"] == spent.tokens["cache_write"]
    assert spent.ends["gauge"] == RUN_1919[-1][1] and spent.ends["cost"] == RUN_1919[-1][1]


def test_a_run_that_resumes_a_session_counts_its_own_turns_from_where_the_session_stood():
    (tokens, _), (later, _) = RUN_1919[5], RUN_1919[6]
    parked = only(ledger(reports_of("claude-code", [INIT, *events(claude(tokens, 0.6))], "s", "")))
    base = {"s": {"gauge": parked.ends["gauge"], "models": {parked.model: parked.ends}}}
    resumed = reports_of("claude-code", [INIT, *events(claude(later, 0.7), claude(later, 0.8))], "s", "")
    spent = only(ledger(resumed, base))
    assert spent.reported_usd == pytest.approx(0.2), "the parked run's 0.6 is its own"
    restarted = only(ledger(reports_of("claude-code", [INIT, *events(claude(later, 0.1))], "s", ""), base))
    assert spent.tokens["output"] == 2 * later[1]
    assert restarted.reported_usd == pytest.approx(0.1), "a process that did not restore the total starts from 0"


def test_claude_code_models_are_each_models_totals_and_a_resumed_run_takes_its_share_of_each():
    def turn(opus: tuple, haiku: tuple, session: str = "s") -> dict:
        models = {
            "claude-opus-5-5": {"inputTokens": opus[0], "outputTokens": opus[1], "costUSD": opus[2]},
            "claude-haiku-5-5": {"inputTokens": haiku[0], "outputTokens": haiku[1], "costUSD": haiku[2]},
        }
        return claude((1, 10, 0, 0), opus[2] + haiku[2], models=models, session_id=session)

    first = ledger(reports_of("claude-code", events(turn((10, 100, 1.0), (5, 5, 0.01))), "run-session", ""))
    assert {item.session for item in first} == {"s"}, "the report's session wins over the run's"
    bases = {"s": {"gauge": 1.01, "models": {item.model: item.ends for item in first}}}
    later = events(turn((15, 160, 1.5), (5, 5, 0.01)), turn((18, 200, 2.0), (9, 8, 0.03)))
    spent = {item.model: item for item in ledger(reports_of("claude-code", later, "", ""), bases)}
    assert spent["claude-opus-5-5"].reported_usd == pytest.approx(1.0)
    assert spent["claude-opus-5-5"].tokens == {"input": 8, "output": 100}
    assert spent["claude-haiku-5-5"].reported_usd == pytest.approx(0.02)
    assert spent["claude-haiku-5-5"].tokens == {"input": 4, "output": 3}


def test_codex_reports_the_threads_tokens_so_far_and_no_cost_so_its_model_is_priced_or_not():
    def thread(total: int, cached: int, output: int, last: int) -> dict:
        usage = {"inputTokens": total, "cachedInputTokens": cached, "outputTokens": output, "reasoningOutputTokens": 2}
        return {"usage": usage, "last": {"inputTokens": last, "outputTokens": 0}}

    bodies = events(thread(100, 40, 9, 100), thread(300, 200, 30, 200), thread(50, 0, 5, 55))
    spent = only(ledger(reports_of("codex", bodies, "t", "gpt-5-codex")))
    assert spent.tokens == {"input": 100 + 50, "cache_read": 200, "output": 35, "reasoning": 4}
    assert spent.reported_usd is None and spent.estimated_usd is None, "no list price for the model"
    priced = only(ledger(reports_of("codex", bodies[:1], "t", "claude-sonnet-4-6")))
    assert priced.estimated_usd == pytest.approx((60 * 3 + 9 * 15 + 40 * 0.3) / 1e6)


def test_opencode_steps_add_up_by_the_model_each_names():
    bodies = events(
        {"usage": {"input": 10, "output": 4, "reasoning": 1, "cache": {"read": 7}}, "cost": 0.25, "model": "m-a"},
        {"usage": {"input": 6, "output": 2}, "cost": {"amount": 0.5, "currency": "USD"}, "model": "m-a"},
        {"usage": {"input": 3, "output": 1}, "cost": 0.1},
    )
    spent = {item.model: item for item in ledger(reports_of("opencode", bodies, "", "m-run"))}
    assert spent["m-a"].reported_usd == pytest.approx(0.75)
    assert spent["m-a"].tokens == {"input": 16, "output": 7, "cache_read": 7, "reasoning": 1}
    assert spent["m-run"].reported_usd == pytest.approx(0.1)


def test_a_local_session_is_estimated_until_a_cost_state_reports_and_after_it_again():
    def message(output: int) -> dict:
        return {"source": "local", "scope": "delta", "models": {"claude-opus-5-5": {"outputTokens": output}}}

    state = {
        "source": "local",
        "scope": "total",
        "cost": 0.5,
        "models": {"claude-opus-5-5": {"outputTokens": 1500, "costUSD": 0.45}, "claude-haiku-5-5": {"costUSD": 0.05}},
    }
    live = only(ledger(reports_of(None, events(message(1000), message(500)), "sid", "")))
    assert live.reported_usd is None and live.estimated_usd == pytest.approx(1500 * 20 / 1e6)
    spent = {
        item.model: item
        for item in ledger(reports_of(None, events(message(1000), message(500), state, message(100)), "sid", ""))
    }
    opus = spent["claude-opus-5-5"]
    assert opus.reported_usd == pytest.approx(0.45), "the cost-state holds the messages before it"
    assert opus.estimated_usd == pytest.approx(100 * 20 / 1e6), "the message after it is priced"
    assert opus.tokens == {"output": 1600}
    assert spent["claude-haiku-5-5"].reported_usd == pytest.approx(0.05)


def test_the_usage_a_worker_reported_stands_for_reports_that_were_not_kept():
    usage = {"input_tokens": 14, "output_tokens": 250, "cache_read_input_tokens": 21000, "total_cost_usd": 2.75}
    report = run_spend.usage_report("claude-code", usage, "s", "claude-opus-5-5")
    spent = only(ledger([report], {"s": {"gauge": 2.0, "models": {"claude-opus-5-5": {"cost": 2.0}}}}))
    assert spent.reported_usd == pytest.approx(0.75)
    assert run_spend.usage_report("codex", None, "s", "m") is None
    assert only(ledger([run_spend.usage_report("northwind/acp", {"cost": 0.9}, "", "")])).reported_usd == 0.9


def test_reports_of_nothing_spent_and_odd_bodies_make_no_spend():
    odd = events("text", {"usage": {"input_tokens": "12"}}, {"cost": {"amount": 1, "currency": "EUR"}}, {})
    assert ledger(reports_of("claude-code", odd, "", "")) == []


def test_the_summary_adds_a_runs_sessions_up_by_model_the_dearest_first():
    spent = [
        run_spend.Spend("a", "claude-haiku-5-5", {"input": 5}, None, 0.01, 1, {}),
        run_spend.Spend("a", "claude-opus-5-5", {"input": 10, "output": 2}, 1.0, None, 2, {}),
        run_spend.Spend("b", "claude-opus-5-5", {"input": 1}, 0.5, 0.25, 1, {}),
    ]
    found = summary(spent)
    assert [entry["model"] for entry in found["models"]] == ["claude-opus-5-5", "claude-haiku-5-5"]
    assert found["tokens"] == {"input": 16, "output": 2}
    assert found["reported_usd"] == pytest.approx(1.5) and found["estimated_usd"] == pytest.approx(0.26)
    assert spent[2].cost_usd == pytest.approx(0.75) and spent[0].cost_usd == pytest.approx(0.01)


def test_run_show_says_the_cost_and_how_it_came_then_each_model():
    from evo_agents.hub.run_cli_spend import spend_lines

    tokens = {"input": 10, "cache_read": 2000, "cache_write": 0, "output": 300, "total": 2310}
    spend = {
        "cost_usd": 1.255,
        "reported_usd": 1.25,
        "estimated_usd": 0.005,
        "tokens": tokens,
        "sessions": 1,
        "models": [
            {
                "model": "claude-opus-5-5",
                "cost_usd": 1.25,
                "reported_usd": 1.25,
                "estimated_usd": None,
                "tokens": tokens,
            },
            {"model": "", "cost_usd": 0.005, "reported_usd": None, "estimated_usd": 0.005, "tokens": {"total": 7}},
        ],
    }
    assert spend_lines(spend) == [
        (
            "spend",
            "$1.25 ($1.25 reported, $0.0050 estimated), 2,310 tokens (in 10, cache read 2,000, cache write 0, out 300), in 1 session(s)",
        ),
        ("", "claude-opus-5-5: $1.25, 2,310 tokens"),
        ("", "-: $0.0050 estimated at list prices, 7 tokens"),
    ]
    assert spend_lines(None) == []
