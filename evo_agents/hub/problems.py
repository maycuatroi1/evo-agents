"""The problems of a project as the hub models them (plan problem-oriented-hub, criterion a1): pure functions and
constants, standard library only, so the api, the command line and the core plugin builder share them.
``evo_agents.hub.server.problems`` holds the routes and the writes.

A problem says what hurts, before any solution: its statement, who is hurt (``who``), when it happens (``when``) and
how they cope today (``workaround``). It may name a metric that shows how much it hurts: a key of kind number of the
project's registry of signals, summed up over a window as a goal's metric is (``aggregate`` of AGGREGATES over
``window_days``, of one ``tenant`` when it names one), with no target: a problem is watched, a goal is reached. It
also has a rank among the project's problems (1 is the first), an owner (a member with the writer role or above) and
a status: open, solved or dropped, the last two with the reason it was closed. The writer sets the status; nothing
works it out from a claim or a metric.

Its code is ``P-<number>``, given in order within the project and never given again: a problem is closed, never
deleted. A goal names at most one problem of its project that is not dropped (``evo_agents.hub.goals``).

Every write is a revision, kept: a writer of the project writes the problem whole, and the agent of a run only
proposes one (@builder, proposal kind ``problem``), which a writer approves.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from evo_agents.hub import goals

STATUSES = ("open", "solved", "dropped")
OPEN, SOLVED, DROPPED = STATUSES
CLOSED = (SOLVED, DROPPED)  # the statuses of a closed problem, each with its reason
MAX_STATEMENT_CHARS = 2000
MAX_TEXT_CHARS = 2000  # of who, when and workaround
MAX_REASON_CHARS = 1000  # of why a problem was closed
MAX_NOTE_CHARS = 1000
MAX_RANK = 10_000
AGGREGATES = goals.AGGREGATES  # as a goal's metric sums its key's signals up
DEFAULT_WINDOW_DAYS = goals.DEFAULT_WINDOW_DAYS
MAX_WINDOW_DAYS = goals.MAX_WINDOW_DAYS
MAX_TENANT_CHARS = goals.MAX_TENANT_CHARS
CODE = re.compile(r"P-([1-9][0-9]{0,8})")
REF = r"^P-[1-9][0-9]{0,8}$"  # a problem's code
FIELDS = ("statement", "who", "when", "workaround", "metric", "rank", "owner", "status", "close_reason")  # a revision's
TEXTS = ("statement", "who", "when", "workaround")  # the fields a problem always says something in
CLEARABLE = ("metric", "close_reason")  # the fields a revision may leave empty
METRIC_FIELDS = ("key", "aggregate", "window_days", "tenant")


def code(number: int) -> str:
    """The code of problem ``number``: P-<number>."""
    return f"P-{number}"


def number_of(ref: str) -> int | None:
    """The number ``ref`` names when it is a code (P-<number>); None otherwise."""
    found = CODE.fullmatch(ref)
    return int(found[1]) if found else None


def changed(before: Mapping, after: Mapping) -> list[str]:
    """The fields of FIELDS whose value differs between two revisions (each a mapping of FIELDS), in that order."""
    return [name for name in FIELDS if before.get(name) != after.get(name)]


def metric_text(metric: Mapping | None) -> str:
    """How a metric reads: ``run.failed_rate p50 over 30 days``, with ``for tenant lumen`` when it names one;
    ``no metric`` without one."""
    if not metric:
        return "no metric"
    days = metric.get("window_days") or DEFAULT_WINDOW_DAYS
    text = f"{metric.get('key')} {metric.get('aggregate')} over {days} days"
    return text + (f" for tenant {metric['tenant']}" if metric.get("tenant") else "")


def status_text(problem: Mapping) -> str:
    """A problem's status, with the reason of a closed one: ``solved (the night shift runs it)``."""
    return problem["status"] + (f" ({problem['close_reason']})" if problem.get("close_reason") else "")
