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

A plan written outside the Loop may deliver use cases of the backlog already: one written before they were approved, or
by hand. ``held`` keeps the group of a use case such a plan names out of any build: a plan *names* a use case when its
words (``plan_text``, its non-goals left out) hold the use case's ref as a whole word (``names``: UC ORD-1 is not
named by UC ORD-10). Whoever reads the plans says which count (``evo_agents.hub.server.loop.backlog.split_of``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

DELIVERED, UNDELIVERED, UNKNOWN, DELIVERING = STATES = ("delivered", "undelivered", "unknown", "delivering")
KG, ADMIN, DELIVER = SOURCES = ("kg", "admin", "deliver")  # who put a use case in the ledger
OUT_OF_BACKLOG = (DELIVERED, DELIVERING, UNKNOWN)  # the states that keep an approved use case out of the backlog
BACKLOG_METRIC = "srs.backlog.open"  # a metric key the hub computes, no key of the registry of signals
MAX_GROUP = 15  # use cases in one group before it splits by heading
NOT_DELIVERED_BY_PLAN = ("non_goals",)  # the keys of a plan's body that name what it does not deliver


@dataclass(frozen=True)
class UseCase:
    """An approved use case: ``ref`` is its code, or its key when it has none; ``heading`` the code or title of its
    direct parent, ``headings`` the codes or titles of every ancestor, the outermost first; ``version`` the version of
    the document it was read from (not part of what makes two use cases equal)."""

    ref: str
    title: str
    claim: str | None = None
    heading: str | None = None
    headings: tuple[str, ...] = ()
    version: int | None = field(default=None, compare=False)


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


@dataclass(frozen=True)
class Held:
    """A group of the backlog no build takes: ``plans`` name one of its use cases, and none of them is the Loop's."""

    group: Group
    plans: tuple[str, ...]


def plan_text(body) -> str:
    """The words of a plan's body (its JSON), but its non-goals, in the form refs are compared in (``ref_key``)."""
    found: list[str] = []

    def walk(value) -> None:
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, Mapping):
            for inner in value.values():
                walk(inner)
        elif isinstance(value, list):
            for inner in value:
                walk(inner)

    if isinstance(body, Mapping):
        walk({key: value for key, value in body.items() if key not in NOT_DELIVERED_BY_PLAN})
    return ref_key("\n".join(found))


def names(text: str, ref: str) -> bool:
    """Whether ``text``, in ``ref_key`` form, holds ``ref`` as a whole word."""
    return re.search(rf"(?<!\w){re.escape(ref_key(ref))}(?!\w)", text) is not None


def held(found: Iterable[Group], naming: Mapping[str, Sequence[str]]) -> tuple[list[Group], list[Held]]:
    """``found`` split in the groups a build may take and those plans hold: a group is held when ``naming`` (a use
    case's ``ref_key`` to the plans that name it) names a plan for one of its use cases."""
    free, waiting = [], []
    for group in found:
        plans = sorted({plan for case in group.use_cases for plan in naming.get(ref_key(case.ref), ())})
        if plans:
            waiting.append(Held(group, tuple(plans)))
        else:
            free.append(group)
    return free, waiting


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
    "NOT_DELIVERED_BY_PLAN",
    "OUT_OF_BACKLOG",
    "SOURCES",
    "STATES",
    "UNDELIVERED",
    "UNKNOWN",
    "Group",
    "Held",
    "UseCase",
    "backlog",
    "groups",
    "held",
    "names",
    "open_count",
    "plan_text",
    "ref_key",
    "under",
]
