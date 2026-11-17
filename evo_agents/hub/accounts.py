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

Quota-pacing reads each account's usage windows (``accounts.last_usage``: ``five_hour`` and ``seven_day``, the share of
the limit used, 0 to 1, and when it resets), and a window not reported yet, or whose reset has passed, counts as
unused. It leaves out every account whose five-hour window is at ``five_hour_ceiling_pct`` or over while another is
under it, then every account with no more than ``hard_reserve_pct`` of its weekly window left while another has more,
and takes the one with the most of its weekly window left per hour until that window resets: the quota that expires
soonest is spent first. An account with no weekly window reported counts a whole week ahead. Equal accounts take turns
as in round-robin.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, get_args

QUOTA_PACING = "quota-pacing"
STRATEGIES = ("round-robin", "weighted-round-robin", "fill-first", QUOTA_PACING)
Strategy = Literal["round-robin", "weighted-round-robin", "fill-first", "quota-pacing"]  # STRATEGIES, as a type
assert get_args(Strategy) == STRATEGIES
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
DEFAULT_HARD_RESERVE_PCT = 10  # quota-pacing keeps this share of a weekly window while another account has more
DEFAULT_FIVE_HOUR_CEILING_PCT = (
    90  # quota-pacing leaves an account this hot in its five-hour window while another is not
)
MAX_PCT = 100
FIVE_HOUR, SEVEN_DAY = "five_hour", "seven_day"
WEEK_HOURS = 7 * 24.0

DEFAULT_POLICY = {
    "strategy": DEFAULT_STRATEGY,
    "cooldown_seconds": DEFAULT_COOLDOWN_SECONDS,
    "failover": True,
    "max_failovers": DEFAULT_MAX_FAILOVERS,
    "hard_reserve_pct": DEFAULT_HARD_RESERVE_PCT,
    "five_hour_ceiling_pct": DEFAULT_FIVE_HOUR_CEILING_PCT,
}

# How a run came by its account (run_accounts.how): picked by hand when it was dispatched, bound to the worker that
# runs it, listed by the Curator's charter, taken from the project's pool, every account was cooling down and the
# run took the one whose cooldown ends first, or shared with the run's principal by another member (0106).
CHOICES = ("manual", "worker", "charter", "pool", "cooling", "shared")
# ``needs-relogin`` is only in the view of an account (GET /v1/accounts): an OAuth account whose cooldown is the one
# a refused refresh token sets, which only a new oauth-login ends. ``account_state`` itself says "cooling" for it.
ACCOUNT_STATES = ("ready", "cooling", "paused", "expired", "needs-relogin")


@dataclass(frozen=True)
class Window:
    """A usage window of an account: the share of its limit used, 0 to 1, and when it resets (None: not said)."""

    used: float
    resets_at: datetime | None = None


@dataclass(frozen=True)
class Pacing:
    """The thresholds of quota-pacing, in percent."""

    hard_reserve_pct: int = DEFAULT_HARD_RESERVE_PCT
    five_hour_ceiling_pct: int = DEFAULT_FIVE_HOUR_CEILING_PCT


@dataclass(frozen=True)
class Candidate:
    """An account a run may take, as ``pick`` weighs it."""

    id: int
    name: str
    weight: int = DEFAULT_WEIGHT
    priority: int = DEFAULT_PRIORITY
    rotation: float = 0.0
    five_hour: Window | None = None
    seven_day: Window | None = None


def _moment(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    try:
        found = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo is not None else found.replace(tzinfo=UTC)


def window_of(usage, name: str) -> Window | None:
    """Window ``name`` of an account's usage (``accounts.last_usage``), or None when it lists none. A rejected usage
    whose limit is that window counts it full."""
    if not isinstance(usage, dict):
        return None
    windows = usage.get("windows")
    windows = windows if isinstance(windows, dict) else {}
    found = windows.get(name)
    limited = usage.get("status") == "rejected" and usage.get("rate_limit_type") == name
    if not isinstance(found, dict) and not limited:
        return None
    found = found if isinstance(found, dict) else {}
    share = found.get("utilization")
    used = float(share) if isinstance(share, int | float) and not isinstance(share, bool) else 0.0
    if limited:
        used = 1.0
    resets_at = _moment(found.get("resets_at") or (usage.get("resets_at") if limited else None))
    return Window(min(max(used, 0.0), 1.0), resets_at)


def used_now(window: Window | None, now: datetime) -> float:
    """The share of ``window`` used at ``now``: 0 when none was reported or its reset has passed."""
    if window is None or (window.resets_at is not None and window.resets_at <= now):
        return 0.0
    return window.used


def weekly_pace(candidate: Candidate, now: datetime) -> float:
    """The share of the weekly window of ``candidate`` left per hour until it resets."""
    window = candidate.seven_day
    left = 1.0 - used_now(window, now)
    if window is None or window.resets_at is None or window.resets_at <= now:
        return left / WEEK_HOURS
    hours = max((window.resets_at - now) / timedelta(hours=1), 1 / 60)
    return left / hours


def paced(candidates: list[Candidate], pacing: Pacing, now: datetime) -> list[Candidate]:
    """The candidates quota-pacing weighs, best first, as the module says: under the five-hour ceiling when any is,
    out of the weekly reserve when any is, then by the weekly quota left per hour."""
    ceiling = pacing.five_hour_ceiling_pct / MAX_PCT
    cool = [c for c in candidates if used_now(c.five_hour, now) < ceiling] or list(candidates)
    reserve = pacing.hard_reserve_pct / MAX_PCT
    spare = [c for c in cool if 1.0 - used_now(c.seven_day, now) > reserve] or cool
    return sorted(spare, key=lambda c: (-weekly_pace(c, now), used_now(c.five_hour, now)))


def _stride(candidate: Candidate, strategy: str) -> float:
    return STRIDE / candidate.weight if strategy == "weighted-round-robin" else STRIDE


def usable(candidates: list[Candidate], strategy: str) -> list[Candidate]:
    """The candidates ``strategy`` may give a run: all but those of weight 0 in a weighted pool."""
    if strategy == "weighted-round-robin":
        return [candidate for candidate in candidates if candidate.weight > 0]
    return list(candidates)


def pick(
    candidates: list[Candidate], strategy: str, pacing: Pacing | None = None, now: datetime | None = None
) -> tuple[Candidate, float] | None:
    """The account ``strategy`` gives the next run among ``candidates``, with the rotation it has once taken; None
    when there is none to give. ``pacing`` holds the thresholds of quota-pacing, the defaults when None."""
    pool = usable(candidates, strategy)
    if not pool:
        return None
    if strategy == QUOTA_PACING:  # the best paced, taking turns among equals
        now = now or datetime.now(UTC)
        ranked = paced(pool, pacing or Pacing(), now)
        best = weekly_pace(ranked[0], now)
        pool = [c for c in ranked if abs(weekly_pace(c, now) - best) < 1e-12]
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
