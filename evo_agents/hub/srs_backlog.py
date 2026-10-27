"""The backlog of a goal that aims at an SRS document (plan srs-backlog-loop, criterion a2): pure functions and
constants, standard library only, so the Loop, the api and the command line share them.
``evo_agents.hub.server.loop.backlog`` reads the use cases and the ledger from the hub and calls these.

A use case is *approved* when the document's approved version holds it, or a review approved it in a version still in
review (``srs.use_case.approved``). The ledger (``srs_delivery``) says, for a use case of a document, whether it is
``delivered``, ``undelivered``, ``unknown`` (nobody knows yet) or ``delivering`` (a work item of the Loop is on it),
from which source (``kg``, ``admin``, ``deliver``) and with what evidence. The backlog of a goal is its approved use
cases minus those the ledger says delivered, delivering or unknown: an unknown use case never enters it by itself.

The backlog is split in groups, one build each: by the ``claim`` field of the use case when it has one, else by its
direct parent heading, else one group per use case. A group of more than ``MAX_GROUP`` use cases splits by the direct
parent heading of its use cases when they are in more than one; a group that cannot split that way stays whole.
``srs.backlog.open``, the number of use cases in the backlog, is the metric a goal reads from it (``BACKLOG_METRIC``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

DELIVERED, UNDELIVERED, UNKNOWN, DELIVERING = STATES = ("delivered", "undelivered", "unknown", "delivering")
KG, ADMIN, DELIVER = SOURCES = ("kg", "admin", "deliver")  # who put a use case in the ledger
OUT_OF_BACKLOG = (DELIVERED, DELIVERING, UNKNOWN)  # the states that keep an approved use case out of the backlog
BACKLOG_METRIC = "srs.backlog.open"  # a metric key the hub computes, no key of the registry of signals
MAX_GROUP = 15  # use cases in one group before it splits by heading


@dataclass(frozen=True)
class UseCase:
    """An approved use case: ``ref`` is its code, or its key when it has none; ``heading`` the code or title of its
    direct parent, ``headings`` the codes or titles of every ancestor, the outermost first."""

    ref: str
    title: str
    claim: str | None = None
    heading: str | None = None
    headings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Group:
    """Use cases one build delivers: ``key`` is stable for the same grouping, ``title`` says what they share."""

    key: str
    title: str
    use_cases: tuple[UseCase, ...]


def ref_key(ref: str) -> str:
    """The form two refs are compared in, as ``tables_srs.code_key``: whitespace runs as one space, case folded."""
    return " ".join(ref.split()).casefold()


def under(cases: Iterable[UseCase], heading: str | None) -> list[UseCase]:
    """The use cases below a heading (its code or title, in any level); all of them without one."""
    if not heading:
        return list(cases)
    wanted = ref_key(heading)
    return [case for case in cases if wanted in {ref_key(name) for name in case.headings}]


def backlog(approved: Iterable[UseCase], ledger: dict[str, str]) -> list[UseCase]:
    """The approved use cases whose ledger state (``ledger``, by ``ref_key``) is neither delivered, delivering nor
    unknown, once each, in the order given."""
    found, seen = [], set()
    for case in approved:
        key = ref_key(case.ref)
        if key not in seen and ledger.get(key) not in OUT_OF_BACKLOG:
            seen.add(key)
            found.append(case)
    return found


def _split(key: str, title: str, cases: Sequence[UseCase], limit: int) -> list[Group]:
    """One group, or one per direct parent heading when it holds more than ``limit`` use cases in several."""
    by_heading: dict[str, list[UseCase]] = {}
    for case in cases:
        by_heading.setdefault(case.heading or "", []).append(case)
    if len(cases) <= limit or len(by_heading) < 2:
        return [Group(key, title, tuple(cases))]
    return [
        Group(f"{key}/heading:{ref_key(heading)}", f"{title}, {heading or 'no heading'}", tuple(found))
        for heading, found in by_heading.items()
    ]


def _bucket(case: UseCase) -> tuple[str, str]:
    """(key, title) of the group ``case`` is in before a split: its claim, else its direct parent, else itself."""
    if case.claim and case.claim.strip():
        return f"claim:{ref_key(case.claim)}", case.claim.strip()
    if case.heading:
        return f"heading:{ref_key(case.heading)}", case.heading
    return f"use-case:{ref_key(case.ref)}", case.ref


def groups(cases: Iterable[UseCase], limit: int = MAX_GROUP) -> list[Group]:
    """``cases`` in groups, in the order their first use case comes: by claim, else by direct parent heading, else a
    group each (see the module)."""
    found: dict[str, tuple[str, list[UseCase]]] = {}
    for case in cases:
        key, title = _bucket(case)
        found.setdefault(key, (title, []))[1].append(case)
    return [group for key, (title, members) in found.items() for group in _split(key, title, members, limit)]


def open_count(approved: Iterable[UseCase], ledger: dict[str, str]) -> int:
    """The value of ``srs.backlog.open``: the use cases in the backlog."""
    return len(backlog(approved, ledger))


__all__ = [
    "ADMIN",
    "BACKLOG_METRIC",
    "DELIVER",
    "DELIVERED",
    "DELIVERING",
    "KG",
    "MAX_GROUP",
    "OUT_OF_BACKLOG",
    "SOURCES",
    "STATES",
    "UNDELIVERED",
    "UNKNOWN",
    "Group",
    "UseCase",
    "backlog",
    "groups",
    "open_count",
    "ref_key",
    "under",
]
