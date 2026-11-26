"""What a write of the plugin canvas takes, as values: who writes (``Author``, ``Writer``), one change of an edit
(``Change``) and what it did (``Outcome``), a piece of evidence to add (``EvidenceInput``), the next rev of a claim
(``NextRev``), what an import did (``Report``), and the limits the service holds a write to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from evo_agents.hub.server.canvas.models.content import Content
from evo_agents.hub.server.projects import ProjectAccess

SINK_HEADER = "X-Evo-Sink"  # evo_agents.hub.server.plans.SINK_HEADER
MAX_CLAIMS = 500  # of a canvas, archived ones included
MAX_EVIDENCE = 200  # of a claim, withdrawn ones included
ADDED_KINDS = ("interview", "experiment", "market", "doc", "simulation")  # what a person or an agent adds
MAX_CHANGES = 100  # claims one edit or undo touches
MAX_PROPOSED = 50  # proposals one call makes
MAX_OPEN = 200  # open proposals of a canvas, and the most one call rejects together
NEW_CLAIM_FIELDS = (  # what a proposed new claim may set
    "cell",
    "text",
    "confidence",
    "delivery",
    "position",
    "test_card",
    "use_cases",
    "signal_keys",
    "problems",
    "opportunities",
)
MAX_PROPOSALS = 10  # what one brainstorm proposes at most
MAX_FOCUS_CHARS = 2_000  # of the focus of a brainstorm


@dataclass(frozen=True)
class Author:
    """The provenance of a write: a person, an agent or the hub; the member it is written for, the run of an agent
    with its runtime and model, and the chat message that asked for it."""

    actor: str = "person"
    user_id: int | None = None
    run_id: int | None = None
    runtime: str | None = None
    model: str | None = None
    chat_message_id: int | None = None

    def columns(self) -> dict:
        return {
            "actor": self.actor,
            "user_id": self.user_id,
            "run_id": self.run_id,
            "runtime": self.runtime,
            "model": self.model,
            "chat_message_id": self.chat_message_id,
        }


@dataclass(frozen=True)
class Writer:
    """Who writes, where, and through which sink they read."""

    access: ProjectAccess
    author: Author
    sink: str | None = None


@dataclass(frozen=True)
class Change:
    """One change of an edit: the fields ``values`` sets on claim ``claim`` read at ``base_rev``, or a new claim when
    ``claim`` is None (``values`` then gives its cell and text, and may give the rest)."""

    claim: str | None = None
    base_rev: int | None = None
    values: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Outcome:
    """What an edit or an undo did to one claim: its number, and whether it wrote a rev."""

    number: int
    written: bool


@dataclass(frozen=True)
class NextRev:
    """The next rev of a claim: its content after, the kind of change, why, who writes it, the rev an undo takes back
    and the baseline whose decision writes it."""

    after: Content
    change: str
    reason: str
    author: Author
    undo_of: int | None = None
    baseline_id: int | None = None


@dataclass(frozen=True)
class EvidenceInput:
    """A piece of evidence to add: its kind, which way it points, a summary, a link, the day it was taken, and
    whether it is synthetic (a simulation always is). The hub's own (the value check) also says what it rests on
    (``basis``), whether that is signals the agent of a run pushed (``rests_on_run``), and the label it carries when
    the signals it sums up carry a higher one than the claim's."""

    kind: str
    direction: str
    summary: str
    url: str | None = None
    observed_on: date | None = None
    synthetic: bool = False
    basis: dict | None = None
    rests_on_run: bool = False
    label: dict | None = None


@dataclass
class Report:
    """What an import did, or would do."""

    canvas: str
    canvas_created: bool = False
    created: list[str] = field(default_factory=list)
    edited: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    evidence: int = 0
    problems: list[dict] = field(default_factory=list)

    def warn(self, claim: str | None, message: str) -> None:
        self.problems.append({"level": "warning", "claim": claim, "message": message})
