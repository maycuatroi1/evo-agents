"""The values the decision API of core plugins (API 1.6 and 1.7) returns, and its bounds and words."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from evo_agents.hub import kg_decisions

MAX_TEXT_CHARS = 2000  # a sanitized text of one item: an error, a turn, a summary, a snippet
MAX_EVENT_TAIL = 20  # the events of a run's log a failure's detail carries, the last ones
MAX_EVENT_CHARS = 400  # the text of one of them
MAX_TAIL_BYTES = 8 * 1024  # all of them together
MAX_READ_ITEMS = 500  # the most items one read returns
MAX_FACETS = 2000  # the sessions of the time range a read of corrections looks through
MAX_REASON_CHARS = kg_decisions.MAX_REASON_CHARS
NO_SINK = "project {project} declares no sink {sink} in its policy, or it clears no level its ladder knows"
NO_RUN = "project {project} has no run {run}"
NO_GRAPH = "project {project} has no graph on the hub yet"

DECISION_KINDS = kg_decisions.KINDS


# What the reads return


@dataclass(frozen=True)
class Cleared:
    """What a text read returns: ``items``, those whose label ``sink`` may read, how many ``withheld`` the label (or the
    sink's absence) kept out, and ``reason`` when there are none to give: the project's policy declares no such sink,
    or the read found nothing at all."""

    sink: str
    items: tuple[Any, ...]
    withheld: int = 0
    reason: str | None = None


@dataclass(frozen=True)
class RunEventText:
    """One event of a run's log, redacted and cut."""

    seq: int
    at: datetime
    kind: str
    text: str


@dataclass(frozen=True)
class RunFailure:
    """Why a run failed: its ``error`` redacted and cut, ``failure_cause`` (None while it has none), ``inferred``
    (true when the hub guessed the cause from the error, so a plugin may classify it), and the last of its events."""

    run_id: int
    project: str
    kind: str
    state: str
    error: str | None
    failure_cause: str | None
    inferred: bool
    events: tuple[RunEventText, ...]


@dataclass(frozen=True)
class ReviewNodeText:
    id: str
    kind: str
    name: str | None


@dataclass(frozen=True)
class ReviewItemText:
    """An open item of the review queue: its ``fingerprint`` (what a dismissal names), the ``rule`` that found it, what
    the rule suggests (``action``), the nodes it is about that the sink may read, and a ``snippet`` of their text."""

    fingerprint: str
    rule: str
    action: str
    nodes: tuple[ReviewNodeText, ...]
    snippet: str


@dataclass(frozen=True)
class CorrectionText:
    """A turn of a person that read as a correction: its ``text``, the ``evidence`` that names it
    (``session:<id>:user_turns:<n>``) and the time of its session."""

    text: str
    evidence: str
    at: datetime


@dataclass(frozen=True)
class AcceptedRule:
    """A proposal of the Curator the project accepted (``kind`` proposal), or its change once it merged (``change``)."""

    kind: str
    id: int
    title: str
    summary: str
    at: datetime | None


# What the writes return


@dataclass(frozen=True)
class CauseClassified:
    """The cause of a failed run the plugin set: the ``cause`` now, the ``previous`` one and the ``seq`` of the event
    of the run that says so. The cause is still a guess (``failure_cause_inferred``)."""

    run_id: int
    cause: str
    previous: str | None
    seq: int


@dataclass(frozen=True)
class DecisionTaken:
    """A decision on the graph the plugin took (``state`` active) or proposed (``proposed``), its ``number`` in the
    project and the project's decision ``rev`` it made."""

    number: int
    kind: str
    state: str
    rev: int


@dataclass(frozen=True)
class DismissedItem:
    """A review item the plugin dismissed, which stays dismissed in later builds that find it."""

    fingerprint: str
    rule: str
