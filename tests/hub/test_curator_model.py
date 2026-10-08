"""The night shift's model without Postgres (``evo_agents.hub.curator``): the window, the night a moment belongs to, and
what a run's usage says it cost."""

from datetime import date, datetime

import pytest

from evo_agents.hub import curator


@pytest.mark.parametrize(
    "local, inside, night",
    [
        (datetime(2026, 10, 8, 21, 59), False, date(2026, 10, 7)),
        (datetime(2026, 10, 8, 22, 0), True, date(2026, 10, 8)),
        (datetime(2026, 10, 8, 23, 59), True, date(2026, 10, 8)),
        (datetime(2026, 10, 9, 0, 0), True, date(2026, 10, 8)),
        (datetime(2026, 10, 9, 5, 59), True, date(2026, 10, 8)),
        (datetime(2026, 10, 9, 6, 0), False, date(2026, 10, 8)),
        (datetime(2026, 10, 9, 12, 0), False, date(2026, 10, 8)),
    ],
)
def test_a_schedule_window_past_midnight_belongs_to_the_night_it_opened(local, inside, night):
    assert curator.window_state(local, "22:00", "06:00") == (inside, night)


@pytest.mark.parametrize(
    "local, inside, night",
    [
        (datetime(2026, 10, 8, 0, 59), False, date(2026, 10, 7)),
        (datetime(2026, 10, 8, 1, 0), True, date(2026, 10, 8)),
        (datetime(2026, 10, 8, 4, 59), True, date(2026, 10, 8)),
        (datetime(2026, 10, 8, 5, 0), False, date(2026, 10, 8)),
    ],
)
def test_a_schedule_window_within_one_day(local, inside, night):
    assert curator.window_state(local, "01:00", "05:00") == (inside, night)


@pytest.mark.parametrize("text", ["24:00", "7:30", "07:60", "0730", "", None])
def test_a_schedule_time_of_day_is_hh_mm(text):
    with pytest.raises(ValueError):
        curator.parse_time(text)


@pytest.mark.parametrize(
    "usage, cost",
    [
        ({"total_cost_usd": 0.42, "input_tokens": 9}, 0.42),  # Claude Code: the session's running total
        ({"cost": 0.15, "input": 3}, 0.15),  # opencode
        ({"total_cost_usd": 0.42, "cost": 9.0}, 0.42),
        ({"inputTokens": 900, "outputTokens": 40}, 0.0),  # Codex reports tokens, never a cost
        ({"total_cost_usd": -1}, 0.0),
        ({"total_cost_usd": True}, 0.0),
        ({"total_cost_usd": "0.42"}, 0.0),
        (None, 0.0),
        ([0.42], 0.0),
    ],
)
def test_night_cost_of_a_run_is_what_its_usage_says(usage, cost):
    assert curator.run_cost(usage) == cost


def test_a_budget_in_money_reads_in_cents():
    assert [curator.money(amount) for amount in (0, 0.05, 0.5023, 15, 0.004)] == [
        "$0.00",
        "$0.05",
        "$0.50",
        "$15.00",
        "$0.0040",
    ]
