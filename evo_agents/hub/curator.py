"""The night shift as the hub models it: the charter a project's admins write, the schedule that runs the plans the
charter allows inside its window, and the caps of each run the schedule queues. Pure functions over JSON values and
datetimes, standard library only, so the api, the hub's worker, the command line and the worker daemon share them.
``evo_agents.hub.server.curator`` holds the routes and the job built on them.

A project has at most one charter on the hub, and every revision of it is kept. Only an admin of the project writes
it, with a web session or a machine token; a worker token never reaches it. The charter names the worker on duty,
one of the writer's own workers, and the member who owns that worker is the owner of the project's schedules: the
runs they queue are dispatched by that member, so the rule that a worker takes only its owner's runs holds as it is.

The window is two local times and an IANA time zone. When the end is not after the start, the window runs past
midnight. The night a moment belongs to is the local date the window last opened on (``night_of``), so the hours
after midnight count toward the night that began the evening before. Each minute the hub's job ``hub.fire_schedules``
looks at every schedule that is not paused: inside the window, with no run of the schedule queued or held, fewer runs
this night than ``max_runs_per_night`` and the night's cost under ``night_budget_usd``, it queues a plan run of the
first plan of ``night_plans`` that has a ready step and no active run, pinned to the worker on duty. Outside the window,
or paused, it queues nothing and cancels a run of the schedule still queued.

Each run the schedule queues carries a budget (RunBudget): ``max_usd``, what is left of the night's budget capped by
``run_budget_usd``; ``max_turns``; and ``max_seconds``, the agent time it may use. The run's timeout is ``max_seconds``
plus BUDGET_GRACE_SECONDS, so the cap stops the agent before the timeout does. Claude Code gets ``max_budget_usd`` and
``max_turns`` (``ClaudeAgentOptions``); Codex reports no cost, so a Codex run stops at its time cap. A claimed run's
budget also says what the run spent already (``spent_usd``, ``spent_seconds``), when it goes on from a parked run.

Claude Code 2.1.293 reports ``total_cost_usd`` as the running total of the session: each result carries the total so
far, and a resumed session starts from the total its transcript saved, while ``--max-budget-usd`` counts only the
spend of the process it is given to (the description of ``total_cost_usd`` in the CLI's own SDK schema). So a run's
cost is the last total its usage reports (``run_cost``), a session counts once, at its largest total, however many
runs went on in it, and an agent that goes on in a session gets as its budget the cap minus what the session spent.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta

SCHEDULE_KINDS = ("night_shift",)  # the schedules a charter makes; later kinds join this tuple
TIERS = (0, 1, 2, 3)  # of a change the Curator proposes, from what merges by itself to what the owner alone merges
AUTO_MERGE_TIERS = (0,)  # the tiers a charter may let the hub merge; tier 1 waits for a plan of its own
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
MAX_CHARTER_BYTES = 64 * 1024  # the charter as JSON

MAX_NIGHT_BUDGET_USD = 1000.0
MIN_RUN_USD = 0.01  # a night with less than this left queues no run
RUN_MINUTES = (10, 720)  # the agent time one run of the schedule may take
MAX_TURNS = 10_000
MAX_RUNS_PER_NIGHT = 100
MAX_DECISIONS_PER_DAY = 100
MAX_REVIEW_DAYS = 30  # the days of sessions and runs a night's figures may count
CIRCUIT_BREAKER = (1, 10)  # failed runs in a row that stop the night shift
BUDGET_GRACE_SECONDS = 300  # a scheduled run's timeout is its time cap plus this

# What `charter show --json` prints besides the charter body; `charter set` leaves them out of what it sends, and the
# api ignores them in a write, so what was shown can be written back.
CHARTER_META = ("project", "revision", "updated_by", "updated_at", "worker_id", "schedule_owner")
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


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


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
