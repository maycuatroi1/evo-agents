"""Accounts and how a run picks one: the rules both ends of ``evo-agents hub account`` share, and the pick itself.
Standard library only, so the command line reads the bounds without the server; ``docs/credentials.md``, section
"Accounts", describes the design built on it.

An **account** is a member's env secret: one value for one variable, such as a token of ``claude setup-token`` for
CLAUDE_CODE_OAUTH_TOKEN. A member may keep several accounts for the same variable, bound to the same project; a run
gets one of them, and keeps it for its whole life. Each account has a ``weight`` (an integer from 0 to MAX_WEIGHT, 1
by default; weight 0 takes it out of a weighted pool), a ``priority`` (0 to MAX_PRIORITY, 0 by default, the lower the
sooner in a fill-first pool), can be paused and resumed by its owner, and cools down until a time after its runtime
said its usage limit was reached.

A member's **policy** for a project says how runs of theirs there pick among their accounts: ``strategy`` (one of
STRATEGIES, round-robin by default), how long an account cools down when its limit came without a reset time
(``cooldown_seconds``), whether a run that hits the limit goes on with another account (``failover``) and at most how
many times (``max_failovers``). A member without a policy for a project has DEFAULT_POLICY.

``pick`` chooses among the accounts that may be used now. Round-robin and weighted round-robin are one stride
scheduler: each account keeps a ``rotation`` that grows by STRIDE divided by its weight (by STRIDE alone in plain
round-robin) each time a run takes it, and the account with the lowest rotation is next. Taking turns, no account lags
the furthest by more than the longest stride among them; one that does (just added, or back from a pause) counts as
that far behind, so it takes its turn and not a burst of them. Ties go to the lower priority, then the name.
Fill-first takes the lowest priority, then the name.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

STRATEGIES = ("round-robin", "weighted-round-robin", "fill-first")
DEFAULT_STRATEGY = "round-robin"
MIN_COOLDOWN_SECONDS = 60
MAX_COOLDOWN_SECONDS = 7 * 24 * 3600
DEFAULT_COOLDOWN_SECONDS = 3600
MAX_FAILOVERS = 20
DEFAULT_MAX_FAILOVERS = 3
MAX_WEIGHT = 1000
MAX_PRIORITY = 1000
DEFAULT_WEIGHT = 1
DEFAULT_PRIORITY = 0
STRIDE = 1_000_000.0  # how far a run moves an account of weight 1 in the rotation
RENEW_WITHIN_DAYS = 30  # an account that ends this soon is shown with a reminder to renew it

DEFAULT_POLICY = {
    "strategy": DEFAULT_STRATEGY,
    "cooldown_seconds": DEFAULT_COOLDOWN_SECONDS,
    "failover": True,
    "max_failovers": DEFAULT_MAX_FAILOVERS,
}

# How a run came by its account (run_accounts.how): picked by hand when it was dispatched, bound to the worker that
# runs it, listed by the Curator's charter, taken from the project's pool, every account was cooling down and the
# run took the one whose cooldown ends first, or shared with the run's principal by another member (0105).
CHOICES = ("manual", "worker", "charter", "pool", "cooling", "shared")
# ``needs-relogin`` is only in the view of an account (GET /v1/accounts): an OAuth account whose cooldown is the one
# a refused refresh token sets, which only a new oauth-login ends. ``account_state`` itself says "cooling" for it.
ACCOUNT_STATES = ("ready", "cooling", "paused", "expired", "needs-relogin")


@dataclass(frozen=True)
class Candidate:
    """An account a run may take, as ``pick`` weighs it."""

    id: int
    name: str
    weight: int = DEFAULT_WEIGHT
    priority: int = DEFAULT_PRIORITY
    rotation: float = 0.0


def _stride(candidate: Candidate, strategy: str) -> float:
    return STRIDE / candidate.weight if strategy == "weighted-round-robin" else STRIDE


def usable(candidates: list[Candidate], strategy: str) -> list[Candidate]:
    """The candidates ``strategy`` may give a run: all but those of weight 0 in a weighted pool."""
    if strategy == "weighted-round-robin":
        return [candidate for candidate in candidates if candidate.weight > 0]
    return list(candidates)


def pick(candidates: list[Candidate], strategy: str) -> tuple[Candidate, float] | None:
    """The account ``strategy`` gives the next run among ``candidates``, with the rotation it has once taken; None
    when there is none to give."""
    pool = usable(candidates, strategy)
    if not pool:
        return None
    if strategy == "fill-first":
        chosen = min(pool, key=lambda candidate: (candidate.priority, candidate.name))
        return chosen, chosen.rotation
    floor = max(candidate.rotation for candidate in pool) - max(_stride(candidate, strategy) for candidate in pool)

    def turn(candidate: Candidate) -> float:
        return max(candidate.rotation, floor)

    chosen = min(pool, key=lambda candidate: (turn(candidate), candidate.priority, candidate.name))
    return chosen, turn(chosen) + _stride(chosen, strategy)


def account_state(paused_at: datetime | None, cooldown_until: datetime | None, expires_at, now: datetime) -> str:
    """One of ACCOUNT_STATES: expired past its end, paused by its owner, cooling until a time to come, else ready."""
    if expires_at is not None and expires_at <= now:
        return "expired"
    if paused_at is not None:
        return "paused"
    if cooldown_until is not None and cooldown_until > now:
        return "cooling"
    return "ready"
