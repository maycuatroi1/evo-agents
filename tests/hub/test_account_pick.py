"""``evo_agents.hub.accounts.pick``, without a hub: round-robin spreads consecutive runs evenly, weighted round-robin
follows the weights and leaves out an account of weight 0, fill-first takes the lowest priority, and an account that
joins late or comes back takes its turn and not a burst of them."""

from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from evo_agents.hub.accounts import STRIDE, Candidate, account_state, pick


def runs(candidates: list[Candidate], strategy: str, count: int) -> list[str]:
    """The accounts ``count`` consecutive runs take, each moving the one it took as the hub records it."""
    pool = {candidate.id: candidate for candidate in candidates}
    taken = []
    for _ in range(count):
        chosen, rotation = pick(list(pool.values()), strategy)
        pool[chosen.id] = replace(chosen, rotation=rotation)
        taken.append(chosen.name)
    return taken


def accounts(*names: str, **weights: int) -> list[Candidate]:
    return [Candidate(i, name, weight=weights.get(name.replace("-", "_"), 1)) for i, name in enumerate(names, 1)]


def test_round_robin_spreads_consecutive_runs_evenly():
    assert runs(accounts("claude-a", "claude-b", "claude-c"), "round-robin", 7) == [
        "claude-a",
        "claude-b",
        "claude-c",
        "claude-a",
        "claude-b",
        "claude-c",
        "claude-a",
    ]
    # weights mean nothing to plain round-robin, not even 0
    assert Counter(runs(accounts("claude-a", "claude-b", claude_a=5, claude_b=0), "round-robin", 6)) == {
        "claude-a": 3,
        "claude-b": 3,
    }


def test_weighted_round_robin_follows_the_weights_and_weight_zero_leaves_the_pool():
    taken = runs(
        accounts("claude-a", "claude-b", "claude-c", claude_a=3, claude_b=1, claude_c=0), "weighted-round-robin", 8
    )
    assert Counter(taken) == {"claude-a": 6, "claude-b": 2}
    assert taken[:4].count("claude-b") == 1  # spread, not a burst of claude-a first
    assert pick(accounts("claude-a", claude_a=0), "weighted-round-robin") is None


def test_fill_first_takes_the_lowest_priority_then_the_name():
    pool = [
        Candidate(1, "claude-a", priority=5),
        Candidate(2, "claude-c", priority=1),
        Candidate(3, "claude-b", priority=1),
    ]
    assert runs(pool, "fill-first", 3) == ["claude-b"] * 3
    assert pick([], "fill-first") is None


def test_an_account_that_joins_late_takes_its_turn_and_not_a_burst():
    pool = [Candidate(1, "claude-a", rotation=50 * STRIDE), Candidate(2, "claude-b", rotation=50 * STRIDE)]
    pool.append(Candidate(3, "claude-c"))  # new: rotation 0, fifty turns behind
    assert runs(pool, "round-robin", 6).count("claude-c") == 2


def test_the_state_of_an_account():
    now = datetime(2026, 10, 13, tzinfo=UTC)
    later, earlier = now + timedelta(hours=1), now - timedelta(hours=1)
    assert account_state(None, None, None, now) == "ready"
    assert account_state(None, earlier, None, now) == "ready"  # its cooldown is over
    assert account_state(None, later, None, now) == "cooling"
    assert account_state(earlier, later, None, now) == "paused"
    assert account_state(earlier, None, earlier, now) == "expired"
