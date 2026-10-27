"""The goals of a project as the hub models them (plan loop-engine, criterion a2): pure functions and constants,
standard library only, so the api, the command line and the core plugin builder share them.
``evo_agents.hub.server.goals`` holds the routes, the writes and what the Curator reads.

A goal says what the project is for in one statement, and may name how to tell it is reached: a key of kind number of
the project's registry of signals, summed up over a window as a test card of the plugin canvas does it (``aggregate``
of AGGREGATES over ``window_days``, of one ``tenant`` when it names one), compared by ``comparator`` with ``target``.
It may also have a deadline, the canvas whose product it drives, the problem of the project it answers (``P-<number>``,
``evo_agents.hub.problems``; at most one, and not a dropped one) or the opportunity of the project it answers
(``O-<number>``, ``evo_agents.hub.opportunities``; a goal answers a problem or an opportunity, not both), a rank among the project's goals (1 is the first),
an owner (a member with the writer role or above) and a status: active, paused, or closed with a reason (achieved,
dropped, expired). It may aim at one SRS document of the project (``srs_document``, its slug), limited to the use cases
under one heading of it (``srs_heading``, a code or title; never without a document): its backlog is then the document's
approved use cases not delivered yet (``evo_agents.hub.srs_backlog``), and the metric ``srs.backlog.open`` counts them
with no signal pushed. A goal has no budget. A goal without a metric is read by the Curator, and nothing else is done for
it.

Its code is ``G-<number>``, given in order within the project and never given again: a goal is closed, never
deleted. A goal the hub made from a goal of the Curator's charter (``{id, what}``) keeps that id as its alias, which
names it as its code does. An alias is lower case, so ``G-3`` is always a code and never an alias.

Every write is a revision, kept: an admin of the project writes the goal whole, and the agent of a run only proposes
one (@builder, proposal kind ``goal``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping

STATUSES = ("active", "paused", "closed")
ACTIVE, PAUSED, CLOSED = STATUSES
CLOSE_REASONS = ("achieved", "dropped", "expired")  # why a closed goal is closed
AGGREGATES = ("last", "mean", "sum", "count", "p50", "p90")  # as a test card of the plugin canvas sums signals up
COMPARATORS = (">=", ">", "<=", "<")
DEFAULT_WINDOW_DAYS = 90  # of a metric that names none, as a test card's
MAX_WINDOW_DAYS = 90
MAX_STATEMENT_CHARS = 2000  # as long as a goal of the charter (curator.MAX_GOAL_CHARS), so one converts whole
MAX_NOTE_CHARS = 1000
MAX_TENANT_CHARS = 100  # tables_signals.MAX_TENANT_CHARS
MAX_RANK = 10_000
MAX_NUMBER = 999_999_999
ALIAS = r"^[a-z0-9][a-z0-9-]{0,63}$"  # curator.GOAL_ID: the id of a goal of the charter
CODE = re.compile(r"G-([1-9][0-9]{0,8})")
REF = r"^(?:G-[1-9][0-9]{0,8}|[a-z0-9][a-z0-9-]{0,63})$"  # a goal's code or its alias
FIELDS = (
    "statement",
    "metric",
    "deadline",
    "canvas",
    "problem",
    "opportunity",
    "srs_document",
    "srs_heading",
    "rank",
    "owner",
    "status",
    "close_reason",
)  # a revision's
CLEARABLE = (
    "metric",
    "deadline",
    "canvas",
    "problem",
    "opportunity",
    "srs_document",
    "srs_heading",
    "close_reason",
)  # the fields a revision may leave empty
METRIC_FIELDS = ("key", "aggregate", "window_days", "comparator", "target", "tenant")


def code(number: int) -> str:
    """The code of goal ``number``: G-<number>."""
    return f"G-{number}"


def number_of(ref: str) -> int | None:
    """The number ``ref`` names when it is a code (G-<number>); None for an alias."""
    found = CODE.fullmatch(ref)
    return int(found[1]) if found else None


def changed(before: Mapping, after: Mapping) -> list[str]:
    """The fields of FIELDS whose value differs between two revisions (each a mapping of FIELDS), in that order."""
    return [name for name in FIELDS if before.get(name) != after.get(name)]


def _figure(value: float) -> str:
    return f"{value:g}"


def metric_text(metric: Mapping | None) -> str:
    """How a metric reads: ``search.zero_rate p50 over 30 days <= 0.05``, with ``for tenant lumen`` when it names one;
    ``no metric`` without one."""
    if not metric:
        return "no metric"
    days = metric.get("window_days") or DEFAULT_WINDOW_DAYS
    text = (
        f"{metric.get('key')} {metric.get('aggregate')} over {days} days {metric.get('comparator')} "
        f"{_figure(float(metric.get('target') or 0))}"
    )
    return text + (f" for tenant {metric['tenant']}" if metric.get("tenant") else "")


def curator_line(goal: Mapping) -> dict:
    """A goal as the prompt of the Curator's review run lists it (``review.build_review_prompt``): its code, its
    alias, and the statement with the metric, the deadline, the canvas and the problem it names."""
    ref = goal["code"] + (f" ({goal['alias']})" if goal.get("alias") else "")
    what = " ".join(str(goal["statement"]).split())
    if goal.get("metric"):
        what += f"; reached when {metric_text(goal['metric'])}"
    if goal.get("deadline"):
        what += f"; by {goal['deadline']}"
    if goal.get("canvas"):
        what += f"; drives canvas {goal['canvas']}"
    if goal.get("problem"):
        what += f"; answers problem {goal['problem']}"
    if goal.get("opportunity"):
        what += f"; answers opportunity {goal['opportunity']}"
    if goal.get("srs_document"):
        what += f"; delivers the use cases of SRS document {goal['srs_document']}"
    return {"id": ref, "what": what}
