"""The API of the plugin canvas as Pydantic models: what its routes take and answer, and how a canvas, a claim, a
rev and evidence become an answer. Every model is named ``Canvas...`` in the hub's OpenAPI document.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from evo_agents.core_plugins.canvas import rules
from evo_agents.core_plugins.canvas.content import (
    AGGREGATES,
    CARD_FIELDS,
    COMPARATORS,
    MAX_CARD_TEXT_CHARS,
    MAX_SAMPLES,
    MAX_TENANT_CHARS,
    MAX_WINDOW_DAYS,
    Content,
    diff,
)
from evo_agents.core_plugins.canvas.store import CanvasRow, ClaimRow, EvidenceRow, Provenance, RevRow
from evo_agents.core_plugins.canvas.transfer import Report
from evo_agents.core_plugins.canvas.writes import ADDED_KINDS, MAX_CHANGES
from evo_agents.hub.server.projects import LabelIn
from evo_agents.hub.tables_canvas import (
    ACTORS,
    CELLS,
    CHANGES,
    CONFIDENCES,
    DIRECTIONS,
    EVIDENCE_KINDS,
    MAX_DESCRIPTION_CHARS,
    MAX_NAME_CHARS,
    MAX_REASON_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_TEXT_CHARS,
    MAX_URL_CHARS,
    SLUG,
    STATUSES,
)

CODE = r"^(?:[Cc][Ll]-)?[1-9][0-9]{0,8}$"  # content.CODE
Reason = Field("", max_length=MAX_REASON_CHARS, description="why, kept with each rev it writes")
CardText = Field(None, min_length=1, max_length=MAX_CARD_TEXT_CHARS)


# Test cards


class CanvasTestCardIn(BaseModel):
    """A test card: the four fields of a Test Card (hypothesis, test, metric, threshold), how a value check compares
    a signal with it (aggregate, window_days, comparator, min_samples, tenant), and its deadline. Every field may be
    left out; testing needs hypothesis, test and deadline."""

    hypothesis: str | None = CardText
    test: str | None = CardText
    metric: str | None = Field(None, max_length=200, description="a signal's key, or feedback")
    aggregate: Literal[AGGREGATES] | None = None
    window_days: int | None = Field(None, ge=1, le=MAX_WINDOW_DAYS)
    comparator: Literal[COMPARATORS] | None = None
    threshold: float | None = None
    min_samples: int | None = Field(None, ge=1, le=MAX_SAMPLES)
    tenant: str | None = Field(None, min_length=1, max_length=MAX_TENANT_CHARS)
    deadline: date | None = None


class CanvasTestCard(BaseModel):
    hypothesis: str | None
    test: str | None
    metric: str | None
    aggregate: Literal[AGGREGATES] | None
    window_days: int | None
    comparator: Literal[COMPARATORS] | None
    threshold: float | None
    min_samples: int | None
    tenant: str | None
    deadline: date | None


def card_model(card: dict | None) -> CanvasTestCard | None:
    if not card:
        return None
    return CanvasTestCard(**{name: card.get(name) for name in CARD_FIELDS})


# What the routes answer


class CanvasProvenance(BaseModel):
    """Who wrote a rev or a piece of evidence."""

    actor: Literal[ACTORS] = Field(description="a person, an agent or the hub")
    login: str | None = Field(description="the member the write was for; None for the hub")
    run_id: int | None = Field(description="the run of the agent that wrote it")
    runtime: str | None
    model: str | None
    chat_message_id: int | None = Field(description="the chat message that asked for it")


class CanvasContent(BaseModel):
    """A claim at one rev."""

    cell: Literal[CELLS]
    position: int
    text: str
    status: Literal[STATUSES]
    confidence: Literal[CONFIDENCES]
    test_card: CanvasTestCard | None
    archived: bool


class CanvasClaim(BaseModel):
    """A claim as it is now."""

    code: str = Field(description="CL-<number>, unique in the project and never given again")
    number: int
    cell: Literal[CELLS]
    position: int = Field(description="its place in the cell, the lowest first")
    text: str
    status: Literal[STATUSES]
    confidence: Literal[CONFIDENCES]
    test_card: CanvasTestCard | None
    archived: bool
    rev: int = Field(description="the rev it is at: an edit names it as base_rev")
    evidence_count: int = Field(description="the pieces of evidence the caller may read, withdrawn ones included")
    real_support_count: int = Field(description="those that are real and support it, which validated needs")
    label: dict
    created_by: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


class CanvasCell(BaseModel):
    cell: Literal[CELLS]
    claims: list[CanvasClaim]


class CanvasSummary(BaseModel):
    """A canvas, as a list shows it."""

    slug: str
    name: str = Field(description="the product's name")
    description: str
    label: dict
    created_by: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    claims: int = Field(description="its claims that are not archived")
    statuses: dict[str, int] = Field(description="how many of those hold each status")


class CanvasDetail(CanvasSummary):
    """A canvas whole: its nine cells, each with its claims in order."""

    cells: list[CanvasCell]


class CanvasFieldChange(BaseModel):
    field: str = Field(description="a field of a claim, a test card's as test_card.deadline")
    before: Any = None
    after: Any = None


class CanvasRev(BaseModel):
    rev: int
    change: Literal[CHANGES]
    undo_of: int | None = Field(description="the rev an undo took back")
    content: CanvasContent
    changes: list[CanvasFieldChange] = Field(description="what differs from the rev before it")
    provenance: CanvasProvenance
    reason: str
    created_at: datetime


class CanvasEvidence(BaseModel):
    id: int
    kind: Literal[EVIDENCE_KINDS]
    direction: Literal[DIRECTIONS]
    summary: str
    url: str | None
    observed_on: date | None = Field(description="the day it was taken")
    synthetic: bool
    provenance: CanvasProvenance
    created_at: datetime
    confirmed_by: str | None = Field(description="the writer who vouched for what an agent added")
    confirmed_at: datetime | None
    withdrawn_at: datetime | None
    withdrawn_by: str | None
    withdrawn_reason: str | None
    real: bool = Field(description="whether it is real evidence: one that supports its claim lets it be validated")
    not_real_because: str | None


class CanvasClaimDetail(CanvasClaim):
    """A claim whole: every rev with its provenance and diff, and its evidence."""

    canvas: str
    revs: list[CanvasRev]
    evidence: list[CanvasEvidence]
    hidden_evidence: int = Field(description="pieces of evidence above what the caller may read, not shown")


class CanvasDiff(BaseModel):
    claim: str
    base: int
    head: int
    base_content: CanvasContent
    head_content: CanvasContent
    changes: list[CanvasFieldChange]


class CanvasEditResult(BaseModel):
    """The claims an edit or an undo named, as they are now, in its order."""

    claims: list[CanvasClaim]
    written: list[str] = Field(description="the codes of the claims it wrote a rev of")


class CanvasImportResult(BaseModel):
    dry_run: bool
    canvas: str
    canvas_created: bool
    created: list[str]
    edited: list[str]
    unchanged: list[str]
    evidence: int = Field(description="pieces of evidence added")
    problems: list[dict] = Field(description="warnings: what was left out, by claim")


class CanvasExportEvidence(BaseModel):
    id: int
    kind: Literal[EVIDENCE_KINDS]
    direction: Literal[DIRECTIONS]
    summary: str
    url: str | None
    observed_on: date | None
    synthetic: bool
    actor: Literal[ACTORS]
    confirmed: bool
    withdrawn: bool


class CanvasExportClaim(CanvasContent):
    code: str
    rev: int
    evidence: list[CanvasExportEvidence]


class CanvasExportCanvas(BaseModel):
    slug: str
    name: str
    description: str
    label: dict


class CanvasExport(BaseModel):
    """evo-canvas/1: a canvas with every claim, archived ones included, and the evidence the caller may read."""

    format: Literal["evo-canvas/1"]
    exported_at: datetime
    project: str
    canvas: CanvasExportCanvas
    claims: list[CanvasExportClaim]


# What the routes take


class CanvasIn(BaseModel):
    slug: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS, description="the product's name")
    description: str = Field("", max_length=MAX_DESCRIPTION_CHARS)
    label: LabelIn | None = Field(None, description="a label above the project's default; an admin's to give")


class CanvasChangeIn(BaseModel):
    """One change of an edit: the fields it sets on ``claim``, read at ``base_rev``; without ``claim``, a new claim
    with at least a cell and a text. ``test_card`` null takes the card away."""

    claim: str | None = Field(None, pattern=CODE, description="CL-3, or 3; left out: a new claim")
    base_rev: int | None = Field(None, ge=1)
    cell: Literal[CELLS] | None = None
    position: int | None = Field(None, ge=0, le=1_000_000)
    text: str | None = Field(None, min_length=1, max_length=MAX_TEXT_CHARS)
    status: Literal[STATUSES] | None = None
    confidence: Literal[CONFIDENCES] | None = None
    test_card: CanvasTestCardIn | None = None
    archived: bool | None = None

    def values(self) -> dict:
        given = self.model_dump(exclude_unset=True, exclude={"claim", "base_rev"}, mode="json")
        if "test_card" in given and given["test_card"] is not None:
            given["test_card"] = {key: value for key, value in given["test_card"].items() if value is not None}
        return given


class CanvasEditIn(BaseModel):
    changes: list[CanvasChangeIn] = Field(min_length=1, max_length=MAX_CHANGES)
    reason: str = Reason


class CanvasUndoTarget(BaseModel):
    claim: str = Field(pattern=CODE, description="CL-3, or 3")
    rev: int = Field(ge=1, description="the rev to take back, which must be the claim's current rev")


class CanvasUndoIn(BaseModel):
    revs: list[CanvasUndoTarget] = Field(min_length=1, max_length=MAX_CHANGES)
    reason: str = Reason


class CanvasEvidenceIn(BaseModel):
    kind: Literal[ADDED_KINDS] = Field(description="signal and feedback are the hub's to write")
    direction: Literal[DIRECTIONS]
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    url: str | None = Field(None, min_length=1, max_length=MAX_URL_CHARS)
    observed_on: date | None = Field(None, description="the day it was taken")
    synthetic: bool = Field(False, description="it rests on simulated people; a simulation always does")


class CanvasWithdrawIn(BaseModel):
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS)


class CanvasImportIn(BaseModel):
    data: dict = Field(description="evo-canvas/1, as export writes it")
    dry_run: bool = False
    label: LabelIn | None = Field(None, description="the label of a canvas the import makes; an admin's to give")


# Rows to answers


def provenance_model(provenance: Provenance) -> CanvasProvenance:
    return CanvasProvenance(**vars(provenance))


def content_model(content: Content) -> CanvasContent:
    return CanvasContent(**{**content.as_json(), "test_card": card_model(content.test_card)})


def claim_model(row: ClaimRow, evidence: list[EvidenceRow]) -> CanvasClaim:
    return CanvasClaim(
        code=row.code,
        number=row.number,
        cell=row.cell,
        position=row.position,
        text=row.text,
        status=row.status,
        confidence=row.confidence,
        test_card=card_model(row.test_card),
        archived=row.archived_at is not None,
        rev=row.rev,
        evidence_count=len(evidence),
        real_support_count=len(rules.real_support([item.facts() for item in evidence])),
        label=row.label,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        archived_at=row.archived_at,
    )


def summary_values(row: CanvasRow, statuses: dict[str, int]) -> dict:
    return {
        "slug": row.slug,
        "name": row.name,
        "description": row.description,
        "label": row.label,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "archived_at": row.archived_at,
        "claims": sum(statuses.values()),
        "statuses": statuses,
    }


def detail_model(row: CanvasRow, statuses: dict[str, int], claims: list[CanvasClaim]) -> CanvasDetail:
    cells = [CanvasCell(cell=cell, claims=[claim for claim in claims if claim.cell == cell]) for cell in CELLS]
    return CanvasDetail(**summary_values(row, statuses), cells=cells)


def changes_model(before: Content | None, after: Content) -> list[CanvasFieldChange]:
    return [CanvasFieldChange(**change) for change in diff(before, after)]


def rev_models(revs: list[RevRow]) -> list[CanvasRev]:
    models, before = [], None
    for rev in revs:
        models.append(
            CanvasRev(
                rev=rev.rev,
                change=rev.change,
                undo_of=rev.undo_of,
                content=content_model(rev.content),
                changes=changes_model(before, rev.content),
                provenance=provenance_model(rev.provenance),
                reason=rev.reason,
                created_at=rev.created_at,
            )
        )
        before = rev.content
    return models


def evidence_model(row: EvidenceRow) -> CanvasEvidence:
    why = rules.not_real(row.facts())
    return CanvasEvidence(
        id=row.id,
        kind=row.kind,
        direction=row.direction,
        summary=row.summary,
        url=row.url,
        observed_on=row.observed_on,
        synthetic=row.synthetic,
        provenance=provenance_model(row.provenance),
        created_at=row.created_at,
        confirmed_by=row.confirmed_by,
        confirmed_at=row.confirmed_at,
        withdrawn_at=row.withdrawn_at,
        withdrawn_by=row.withdrawn_by,
        withdrawn_reason=row.withdrawn_reason,
        real=why is None,
        not_real_because=why,
    )


def import_model(report: Report, dry_run: bool) -> CanvasImportResult:
    return CanvasImportResult(
        dry_run=dry_run,
        canvas=report.canvas,
        canvas_created=report.canvas_created,
        created=report.created,
        edited=report.edited,
        unchanged=report.unchanged,
        evidence=report.evidence,
        problems=report.problems,
    )
