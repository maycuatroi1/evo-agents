"""The rows of the plugin canvas as the repository reads them: a canvas, a claim as it is now, a rev of a claim with
its provenance, a piece of evidence, and a proposal. Frozen values, which the service decides on and the API's models
answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from evo_agents.hub.server.canvas.models.content import Content, code
from evo_agents.hub.server.canvas.models.rules import Evidence


@dataclass(frozen=True)
class CanvasRow:
    id: int
    slug: str
    name: str
    description: str
    label: dict
    created_by: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


@dataclass(frozen=True)
class ClaimRow:
    """A claim as it is now."""

    id: int
    canvas_id: int
    number: int
    cell: str
    position: int
    text: str
    status: str
    confidence: str
    test_card: dict | None
    rev: int
    label: dict
    created_by: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    use_cases: list[str] = field(default_factory=list)  # the codes of the use cases it links
    signal_keys: list[str] = field(default_factory=list)  # the signal keys it links
    problems: list[str] = field(default_factory=list)  # the codes of the problems of the project it answers
    opportunities: list[str] = field(default_factory=list)  # the codes of the opportunities of the project it links
    delivery: str = "code"  # code or action (tables_canvas.DELIVERIES)

    @property
    def code(self) -> str:
        return code(self.number)

    def content(self) -> Content:
        return Content(
            cell=self.cell,
            position=self.position,
            text=self.text,
            status=self.status,
            confidence=self.confidence,
            test_card=self.test_card,
            archived=self.archived_at is not None,
            use_cases=tuple(self.use_cases),
            signal_keys=tuple(self.signal_keys),
            problems=tuple(self.problems),
            opportunities=tuple(self.opportunities),
            delivery=self.delivery,
        )


@dataclass(frozen=True)
class Provenance:
    """Who wrote a rev or a piece of evidence: a person, an agent or the hub; the member it was written for (their
    login), the run with its runtime and model, and the chat message that asked for it."""

    actor: str
    login: str | None
    run_id: int | None
    runtime: str | None
    model: str | None
    chat_message_id: int | None


@dataclass(frozen=True)
class RevRow:
    rev: int
    content: Content
    change: str
    undo_of: int | None
    provenance: Provenance
    reason: str
    created_at: datetime
    baseline_id: int | None = None  # the baseline whose decision wrote it


@dataclass(frozen=True)
class EvidenceRow:
    id: int
    claim_id: int
    kind: str
    direction: str
    summary: str
    url: str | None
    observed_on: date | None
    synthetic: bool
    label: dict
    provenance: Provenance
    created_at: datetime
    confirmed_by: str | None
    confirmed_at: datetime | None
    withdrawn_at: datetime | None
    withdrawn_by: str | None
    withdrawn_reason: str | None
    basis: dict | None = None  # what a piece the hub wrote rests on: a check's figures, or the item judged
    rests_on_run: bool = False  # the hub wrote it from signals the agent of a run pushed

    def facts(self) -> Evidence:
        """What the rule of validated reads of it."""
        return Evidence(
            id=self.id,
            kind=self.kind,
            direction=self.direction,
            synthetic=self.synthetic,
            actor=self.provenance.actor,
            confirmed=self.confirmed_by is not None,
            withdrawn=self.withdrawn_at is not None,
            rests_on_run=self.rests_on_run,
        )


@dataclass(frozen=True)
class ProposalRow:
    id: int
    kind: str
    claim_id: int | None
    claim: str | None  # the claim's code: the one it changes, or the one accepting it made
    base_rev: int | None
    fields: dict  # the fields of the claim it sets, checked
    reason: str
    synthetic: bool
    provenance: Provenance
    created_at: datetime
    status: str
    rev: int | None
    decided_by: str | None
    decided_at: datetime | None
    decided_reason: str | None
    evidence_id: int | None = None  # the evidence a value check proposed it on
