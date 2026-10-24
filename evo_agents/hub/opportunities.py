"""The opportunities of a project as the hub models them (plan opportunity-intake, criterion a1): pure functions and
constants, standard library only, so the api, the command line and the core plugin builder share them.
``evo_agents.hub.server.opportunities`` holds the routes and the writes.

An opportunity says what the project could win: its statement, who benefits (``beneficiary``) and what makes it
reachable now (``leverage``). It may name a window (``window_until``, a date, and ``window_note``), a metric as a
problem's (a key of the project's registry of signals, summed up over a window, with no target), a rank among the
project's opportunities (1 is the first), an owner (a member with the writer role or above) and a status: open,
captured, missed or dropped, the last three with the reason it was closed. The writer sets the status; nothing works
it out from a window or a metric.

It answers problems: at most MAX_ANSWERS codes, ``P-3`` for a problem of its own project and ``<project>/P-3`` for one
of another project the writer holds a grant on. A reader without a grant on that other project sees how many such
references the opportunity holds and nothing of them.

Its code is ``O-<number>``, given in order within the project and never given again: an opportunity is closed, never
deleted. Every write is a revision, kept: a writer of the project writes the opportunity whole, and the agent of a run
only proposes one (@builder, proposal kind ``opportunity``), which a writer approves.

Its evidence is a problem's with seven more signals (SIGNALS), numbered E-<number> within the opportunity.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from evo_agents.hub import problems

STATUSES = ("open", "captured", "missed", "dropped")
OPEN, CAPTURED, MISSED, DROPPED = STATUSES
CLOSED = (CAPTURED, MISSED, DROPPED)  # the statuses of a closed opportunity, each with its reason
MAX_STATEMENT_CHARS = problems.MAX_STATEMENT_CHARS
MAX_TEXT_CHARS = problems.MAX_TEXT_CHARS  # of beneficiary, leverage and window_note
MAX_REASON_CHARS = problems.MAX_REASON_CHARS
MAX_NOTE_CHARS = problems.MAX_NOTE_CHARS
MAX_RANK = problems.MAX_RANK
MAX_ANSWERS = 20  # problems one opportunity answers
CODE = re.compile(r"O-([1-9][0-9]{0,8})")
REF = r"^O-[1-9][0-9]{0,8}$"  # an opportunity's code
ANSWER = re.compile(r"(?:([a-z0-9][a-z0-9-]{0,99})/)?(P-[1-9][0-9]{0,8})")
FIELDS = (
    "statement",
    "beneficiary",
    "leverage",
    "window_until",
    "window_note",
    "answers",
    "metric",
    "rank",
    "owner",
    "status",
    "close_reason",
)  # a revision's
TEXTS = ("statement", "beneficiary", "leverage")  # the fields an opportunity always says something in
CLEARABLE = ("window_until", "window_note", "metric", "close_reason")  # the fields a revision may leave empty

# The evidence of an opportunity (plan opportunity-intake, criterion a2): a line of a source that shows it is there.
OPPORTUNITY_SIGNALS = (
    "wish",
    "external_demand",
    "window",
    "reusable_asset",
    "pull",
    "intro",
    "idle_capacity",
)  # the signals only an opportunity's line shows
SIGNALS = problems.SIGNALS + OPPORTUNITY_SIGNALS
MAX_EVIDENCE = problems.MAX_EVIDENCE  # lines of one opportunity
MAX_PROPOSED_EVIDENCE = problems.MAX_PROPOSED_EVIDENCE  # lines one proposal of @builder carries


def code(number: int) -> str:
    """The code of opportunity ``number``: O-<number>."""
    return f"O-{number}"


def number_of(ref: str) -> int | None:
    """The number ``ref`` names when it is a code (O-<number>); None otherwise."""
    found = CODE.fullmatch(ref)
    return int(found[1]) if found else None


def answer_of(text: str) -> tuple[str | None, int] | None:
    """(the project, the problem's number) an answer names: ``P-3`` is (None, 3) and ``lumen/P-3`` is ("lumen", 3);
    None when ``text`` is neither."""
    found = ANSWER.fullmatch(text.strip())
    return (found[1], int(found[2][2:])) if found else None


def answer_text(project: str | None, number: int) -> str:
    """How an answer reads: ``P-3``, or ``lumen/P-3`` for a problem of another project."""
    return problems.code(number) if project is None else f"{project}/{problems.code(number)}"


def changed(before: Mapping, after: Mapping) -> list[str]:
    """The fields of FIELDS whose value differs between two revisions (each a mapping of FIELDS), in that order."""
    return [name for name in FIELDS if before.get(name) != after.get(name)]


def status_text(opportunity: Mapping) -> str:
    """An opportunity's status, with the reason of a closed one: ``captured (the pilot signed)``."""
    return opportunity["status"] + (f" ({opportunity['close_reason']})" if opportunity.get("close_reason") else "")


def window_text(opportunity: Mapping) -> str:
    """The window as it reads: ``until 2026-12-01 (the fair)``; ``no window`` without a date or a note."""
    until, note = opportunity.get("window_until"), opportunity.get("window_note")
    if not until and not note:
        return "no window"
    return " ".join(part for part in (f"until {until}" if until else "", f"({note})" if note else "") if part)
