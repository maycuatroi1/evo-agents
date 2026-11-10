"""The night shift as the hub models it: the charter a project's admins write, the schedule that runs the plans the
charter allows inside its window, and the caps of each run the schedule queues. Pure functions over JSON values and
datetimes, standard library only, so the api, the hub's worker, the command line and the worker daemon share them.
``evo_agents.hub.server.curator`` holds the routes and the job built on them.

A project has at most one charter on the hub, and every revision of it is kept. Only an admin of the project writes
it, with a web session or a machine token; a worker token never reaches it. The charter names no worker: the night
shift runs on the pool of the project's default agent, and the member who owns that pool is the owner of the project's
schedules: the runs they queue are dispatched by that member, so the rule that a worker takes only its owner's runs
holds as it is.

The window is two local times and an IANA time zone. When the end is not after the start, the window runs past midnight.
The night a moment belongs to is the local date the window last opened on (``night_of``), so the hours after midnight
count toward the night that began the evening before. Each minute the hub's job ``hub.fire_schedules`` looks at every
schedule that is not paused: inside the window, with no run of the schedule queued or held, fewer runs this night than
``max_runs_per_night`` and the night's cost under ``night_budget_usd``, it queues a plan run of the first plan of
``night_plans`` that has a ready step and no active run, on a node of that pool. Outside the window it queues only
the runs of the Curator's changes (their Builder, judge and replay runs), within the same caps and counted toward the
night that last opened, and cancels any other run of the schedule still queued; paused, it queues nothing and cancels
every run of the schedule still queued.

Each run the schedule queues carries a budget (RunBudget): ``max_usd``, what is left of the night's budget capped by
``run_budget_usd``; ``max_turns``; and ``max_seconds``, the agent time it may use. The run's timeout is ``max_seconds``
plus BUDGET_GRACE_SECONDS, so the cap stops the agent before the timeout does. Claude Code gets ``max_budget_usd`` and
``max_turns`` (``ClaudeAgentOptions``); Codex reports no cost, so a Codex run stops at its time cap. A claimed run's
budget also says what the run spent already (``spent_usd``, ``spent_seconds``), when it goes on from a parked run.

At the charter's ``brief_at``, in its time zone, the hub sends the schedule's owner the morning brief of the night
(``brief_due``, ``evo_agents.hub.server.brief``).

The charter's ``answer_by`` (ANSWERERS) says who answers the Curator's proposals: ``owner``, the default, leaves them
to the project's admins; ``agent`` has the hub open a thread for each proposal in the project's triage chat, where the
project's default agent answers it for the schedule's owner (``evo_agents.hub.server.curator_threads``).

The circuit breaker stops a night shift that keeps failing: once the charter's ``circuit_breaker.max_failed_in_a_row``
jobs of one night failed in a row (``failed_in_a_row``), a run of the schedule that ended failed or a merged change of
the project whose figures the hub found worse (an outcome ``revert``), the hub pauses the project's schedules and tells
their owner (``evo_agents.hub.server.outcomes``). A run that ended done breaks the row; a cancelled one, or a lost one
the hub tries again, leaves it as it is, and so does a run that failed of a cause of INFRA_CAUSES: the agent did not
get it wrong, a dependency of the run did. Resuming the night shift starts the count again.

A dependency that fails so is held off alone (``outages``): the hub (``hub``, for HUB_CAUSES) or the runtime of the run
with its account (``runtime:<name>``). The kinds of run that use it wait the backoff of the charter's
``circuit_breaker.infra.backoff_minutes`` (INFRA_BACKOFF_MINUTES by default) for the failures in a row, with a jitter
of INFRA_JITTER either way that the dependency and its last failed run fix, then one run of them goes as a probe
(half-open): ended done, or failed of a cause of the agent's, it closes the dependency; failed of an infra cause again,
the next backoff starts. A dependency that had ``max_probes_per_day`` probes (INFRA_PROBES_PER_DAY by default) in the
last day waits until the oldest of them is a day old (``evo_agents.hub.server.infra_breaker``).

Claude Code 2.1.293 reports ``total_cost_usd`` as the running total of the session: each result carries the total so
far, and a resumed session starts from the total its transcript saved, while ``--max-budget-usd`` counts only the
spend of the process it is given to (the description of ``total_cost_usd`` in the CLI's own SDK schema). So a run's
cost is the last total its usage reports (``run_cost``), a session counts once, at its largest total, however many
runs went on in it, and an agent that goes on in a session gets as its budget the cap minus what the session spent.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal, get_args

ScheduleKind = Literal["night_shift"]
SCHEDULE_KINDS: tuple[str, ...] = get_args(ScheduleKind)  # the schedules a charter makes; later kinds join this tuple
Tier = Literal[0, 1, 2, 3]
TIERS: tuple[int, ...] = get_args(
    Tier
)  # of a change the Curator proposes, from what merges by itself to what the owner alone merges
AutoMergeTier = Tier
AUTO_MERGE_TIERS = TIERS  # the tiers a charter may let the hub merge (auto_merge); none unless it names them
Answerer = Literal["owner", "agent"]
ANSWERERS: tuple[str, ...] = get_args(
    Answerer
)  # who answers the proposals (answer_by): an admin, or the project's agent
DEFAULT_RUNTIME = "claude-code"

TIME_OF_DAY = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"  # HH:MM, 24 hours
TIME_ZONE = r"^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*$"  # an IANA name, checked against Postgres's list
GOAL_ID = r"^[a-z0-9][a-z0-9-]{0,63}$"
MAX_TIME_ZONE_CHARS = 64
MAX_GOALS = 20
MAX_GOAL_CHARS = 2000
MAX_NIGHT_PLANS = 50
MAX_PROTECTED_PATHS = 200
MAX_PATH_CHARS = 500
MAX_HIDDEN_CHECKS = 50
MAX_CHECK_CHARS = 2000
MAX_ENV_SECRETS = 20  # env secrets of the owner a charter lets the Curator's runs have
MAX_CHARTER_BYTES = 64 * 1024  # the charter as JSON

MAX_NIGHT_BUDGET_USD = 1000.0
MIN_RUN_USD = 0.01  # a night with less than this left queues no run
RUN_MINUTES = (10, 720)  # the agent time one run of the schedule may take
MAX_TURNS = 10_000
MAX_RUNS_PER_NIGHT = 100
MAX_DECISIONS_PER_DAY = 100
MAX_REVIEW_DAYS = 30  # the days of sessions and runs a night's figures may count
CIRCUIT_BREAKER = (1, 10)  # failed runs in a row that stop the night shift
# The causes of a failed run that say a dependency of the run failed, not the agent: they never count toward the
# circuit breaker. A cause that is empty or not one of them is the agent's.
INFRA_CAUSES = (
    "account_auth",
    "rate_limit",
    "network",
    "hub_5xx",
    "worker_stopped",
    "lease_lost",
    "runtime",
    "missing_tool",
)
HUB_CAUSES = ("network", "hub_5xx")  # of these the dependency is the hub, which every kind of run uses
HUB = "hub"
INFRA_BACKOFF_MINUTES = (
    15,
    30,
    60,
    240,
)  # circuit_breaker.infra.backoff_minutes by default: the nth failure waits the nth
INFRA_PROBES_PER_DAY = 6  # circuit_breaker.infra.max_probes_per_day by default
INFRA_BACKOFFS = (1, 10)  # the steps of backoff_minutes
INFRA_BACKOFF_RANGE = (1, 24 * 60)  # the minutes of one step
INFRA_PROBES = (1, 48)  # max_probes_per_day
INFRA_JITTER = 0.2  # a backoff is its minutes times 1 - INFRA_JITTER to 1 + INFRA_JITTER
INFRA_LOOKBACK = timedelta(days=2)  # the ended runs of a schedule the dependencies are read from
PROBE_DAY = timedelta(days=1)
BUDGET_GRACE_SECONDS = 300  # a scheduled run's timeout is its time cap plus this
BRIEF_GRACE = timedelta(hours=3)  # a morning brief the hub could not send this long after brief_at waits a day

# What `charter show --json` prints besides the charter body; `charter set` leaves them out of what it sends, and the
# api ignores them in a write, so what was shown can be written back.
CHARTER_META = ("project", "revision", "updated_by", "updated_at", "schedule_owner")
BUDGET_KEYS = ("max_usd", "max_turns", "max_seconds")  # a run's caps (runs.budget)
SPENT_KEYS = ("spent_usd", "spent_seconds")  # added to them in a claim

_TIME = re.compile(TIME_OF_DAY)


def parse_time(text: str) -> time:
    """``time(22, 0)`` for ``"22:00"``; ValueError for anything that is not HH:MM."""
    if not isinstance(text, str) or not _TIME.match(text):
        raise ValueError(f"{text!r} is not a time of day as HH:MM, 00:00 to 23:59")
    hours, minutes = text.split(":")
    return time(int(hours), int(minutes))


def in_window(local: time, start: time, end: time) -> bool:
    """Whether the local time ``local`` falls in the window from ``start`` (included) to ``end`` (left out), which runs
    past midnight when ``end`` is not after ``start``."""
    if start < end:
        return start <= local < end
    return local >= start or local < end


def night_of(local_now: datetime, start: time) -> date:
    """The night ``local_now`` (a local time without zone) belongs to: the local date the window last opened on."""
    opened_today = local_now.time() >= start
    return local_now.date() if opened_today else local_now.date() - timedelta(days=1)


def window_state(local_now: datetime, start: str, end: str) -> tuple[bool, date]:
    """(whether ``local_now`` is inside the window, the night it belongs to) for a window from ``start`` to ``end``,
    both HH:MM. Outside the window the night is the last one that opened."""
    opens, closes = parse_time(start), parse_time(end)
    return in_window(local_now.time(), opens, closes), night_of(local_now, opens)


def brief_due(local_now: datetime, brief_at: str) -> bool:
    """Whether the morning brief of the local day of ``local_now`` (a local time without zone) is due: from
    ``brief_at`` (HH:MM) of that day until BRIEF_GRACE later, so a hub that was down at brief_at still sends it soon
    after, and a charter written in the afternoon waits for the next morning."""
    opens = datetime.combine(local_now.date(), parse_time(brief_at))
    return opens <= local_now < opens + BRIEF_GRACE


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def failed_in_a_row(bad: list[bool]) -> int:
    """How many jobs at the end of ``bad`` (a night's jobs, oldest first, True for one that failed or was reverted)
    failed one after the other."""
    count = 0
    for failed in reversed(bad):
        if not failed:
            break
        count += 1
    return count


# Infrastructure failures


def is_infra(cause: str | None) -> bool:
    """Whether a run that failed of ``cause`` failed for a dependency of it (INFRA_CAUSES), not for its agent."""
    return cause in INFRA_CAUSES


def dependency_of(cause: str | None, runtime: str) -> str | None:
    """The dependency a run on ``runtime`` that failed of ``cause`` points at: HUB, ``runtime:<runtime>`` (the runtime
    and the account it ran with), or None for a cause of the agent's."""
    if not is_infra(cause):
        return None
    return HUB if cause in HUB_CAUSES else f"runtime:{runtime}"


def infra_settings(charter: Mapping | None) -> tuple[tuple[int, ...], int]:
    """(backoff_minutes, max_probes_per_day) of the charter's ``circuit_breaker.infra``, the defaults for each one a
    charter written before it lacks."""
    breaker = (charter or {}).get("circuit_breaker") if isinstance(charter, Mapping) else None
    infra = breaker.get("infra") if isinstance(breaker, Mapping) else None
    infra = infra if isinstance(infra, Mapping) else {}
    found = infra.get("backoff_minutes")
    steps = (
        tuple(found)
        if isinstance(found, list)
        and found
        and all(isinstance(m, int) and not isinstance(m, bool) and m > 0 for m in found)
        else INFRA_BACKOFF_MINUTES
    )
    most = infra.get("max_probes_per_day")
    most = most if isinstance(most, int) and not isinstance(most, bool) and most >= 1 else INFRA_PROBES_PER_DAY
    return steps, most


def backoff(failures: int, steps: tuple[int, ...], key: str) -> timedelta:
    """How long a dependency that failed ``failures`` times in a row waits before its probe: the step of ``steps`` for
    that failure (the last one past their end), times a jitter within INFRA_JITTER that ``key`` fixes, so the minute
    the job runs again finds the same time."""
    minutes = steps[min(max(failures, 1), len(steps)) - 1]
    share = int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big") / 2**32
    return timedelta(minutes=minutes * (1 - INFRA_JITTER + 2 * INFRA_JITTER * share))


@dataclass(frozen=True)
class Ended:
    """A run of the schedule that ended done or failed, as ``outages`` reads it."""

    run_id: int
    queued_at: datetime
    finished_at: datetime
    runtime: str
    failed: bool
    cause: str | None


@dataclass(frozen=True)
class Outage:
    """A dependency whose last runs failed of an infra cause: how often in a row, the last one, when its probe may go,
    and the probes of the last day."""

    dependency: str
    cause: str  # the last failure's
    failures: int
    since: datetime  # when the first failure in a row ended
    last_run_id: int
    retry_at: datetime  # no run that uses the dependency is queued before
    probes: int  # runs queued while it was failing, in the last PROBE_DAY
    exhausted: bool  # probes reached max_probes_per_day: retry_at waits for the oldest to be a day old

    def held(self, now: datetime) -> bool:
        """Whether the kinds of run that use the dependency wait at ``now``; once False, the next of them is a probe."""
        return now < self.retry_at


def outages(ended: list[Ended], now: datetime, steps: tuple[int, ...], most: int, key: str = "") -> dict[str, Outage]:
    """The dependencies whose runs in ``ended`` (oldest end first) failed of an infra cause since they last worked, by
    name. A run that ended done, or failed of the agent's cause, reached the hub and its runtime: both work again. Of
    the failures in a row of a dependency, each but the first is a probe; ``key`` (the schedule) seeds the jitter."""
    rows: dict[str, list[Ended]] = {}
    for run in sorted(ended, key=lambda r: (r.finished_at, r.run_id)):
        dependency = dependency_of(run.cause, run.runtime) if run.failed else None
        if dependency is None:
            rows.pop(HUB, None)
            rows.pop(f"runtime:{run.runtime}", None)
            continue
        rows.setdefault(dependency, []).append(run)
    found = {}
    for dependency, failed in rows.items():
        last = failed[-1]
        recent = sorted(run.queued_at for run in failed[1:] if run.queued_at > now - PROBE_DAY)
        retry_at = last.finished_at + backoff(len(failed), steps, f"{key}:{dependency}:{last.run_id}")
        exhausted = len(recent) >= most
        if exhausted:
            retry_at = max(retry_at, recent[len(recent) - most] + PROBE_DAY)
        found[dependency] = Outage(
            dependency=dependency,
            cause=last.cause or "",
            failures=len(failed),
            since=failed[0].finished_at,
            last_run_id=last.run_id,
            retry_at=retry_at,
            probes=len(recent),
            exhausted=exhausted,
        )
    return found


def kind_runtimes(charter: Mapping, kind: str) -> tuple[str, ...]:
    """The runtimes the night shift runs ``kind`` on by the charter: the reviewer's for review and history, the
    builder's for plan and replay, those of ``probes.models`` for probe. The judge's the hub picks itself (empty)."""

    def role(name: str) -> str:
        found = charter.get(name) if isinstance(charter.get(name), Mapping) else {}
        return found.get("runtime") or DEFAULT_RUNTIME

    if kind in ("review", "history"):
        return (role("reviewer"),)
    if kind in ("plan", "replay"):
        return (role("builder"),)
    if kind == "probe":
        models = (charter.get("probes") or {}).get("models") or []
        named = [item.get("runtime") or DEFAULT_RUNTIME for item in models if isinstance(item, Mapping)]
        return tuple(dict.fromkeys(named)) or (role("builder"),)
    return ()


def run_cost(usage) -> float:
    """What a run's usage says it cost in USD: Claude Code's ``total_cost_usd``, else opencode's ``cost``; 0 for usage
    that names neither, such as Codex's, which reports tokens only."""
    if not isinstance(usage, Mapping):
        return 0.0
    for key in ("total_cost_usd", "cost"):
        found = _number(usage.get(key))
        if found is not None:
            return max(found, 0.0)
    return 0.0


def budget_of(run: Mapping) -> dict | None:
    """The budget of a run as the claim answered it: its caps and what it spent already, numbers only; None for a run
    without one."""
    found = run.get("budget") if isinstance(run, Mapping) else None
    if not isinstance(found, Mapping):
        return None
    numbers = {key: _number(found.get(key)) for key in (*BUDGET_KEYS, *SPENT_KEYS)}
    if all(numbers[key] is None for key in BUDGET_KEYS):
        return None
    return numbers


def usd_left(budget: Mapping, spent_usd: float) -> float | None:
    """What is left of the budget's ``max_usd`` once ``spent_usd`` is spent, never below MIN_RUN_USD (a CLI given 0
    may not stop at all); None without a cost cap."""
    cap = budget.get("max_usd")
    if cap is None:
        return None
    return round(max(cap - max(spent_usd, 0.0), MIN_RUN_USD), 4)


def seconds_left(budget: Mapping, spent_seconds: float) -> float | None:
    """What is left of the budget's ``max_seconds`` once ``spent_seconds`` of agent time are used, at least 0; None
    without a time cap."""
    cap = budget.get("max_seconds")
    if cap is None:
        return None
    return max(cap - max(spent_seconds, 0.0), 0.0)


def money(amount: float) -> str:
    """``$0.05`` for 0.05: cents, or tenths of a cent below a cent."""
    return f"${amount:.2f}" if amount >= 0.01 or amount == 0 else f"${amount:.4f}"
