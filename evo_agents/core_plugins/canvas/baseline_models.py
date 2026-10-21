"""The API of the baselines of a canvas as Pydantic models (``server``; criterion a6 of plan living-lean-canvas): a
baseline with each item it asks, a member's decision on one, and opening one at once.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.core_plugins.canvas.baselines import DECIDES, BaselineRow, ItemRow
from evo_agents.core_plugins.canvas.models import CODE, CanvasEvidence, evidence_model
from evo_agents.core_plugins.canvas.store import EvidenceRow
from evo_agents.hub.tables_canvas import (
    BASELINE_KINDS,
    BASELINE_STATUSES,
    DECISIONS,
    ITEM_KINDS,
    MAX_REASON_CHARS,
    STATUSES,
)

MEMBER_DECISIONS = tuple(sorted({decision for allowed in DECIDES.values() for decision in allowed}))


class CanvasBaselineItem(BaseModel):
    """What a baseline asks of one claim, and the decision on it."""

    claim: str = Field(description="the claim's code")
    kind: Literal[ITEM_KINDS] = Field(
        description="status: members changed its status; value_check: the value check proposes a status; overdue: its "
        "test card's deadline passed without real evidence; new: a claim at assumption, included as it is"
    )
    from_status: Literal[STATUSES] = Field(description="the status in effect when the baseline was proposed")
    at_status: Literal[STATUSES] = Field(description="the status the claim was at")
    to_status: Literal[STATUSES] | None = Field(description="the status it asks; none for an overdue test card")
    claim_rev: int = Field(description="the claim's rev when the baseline was proposed")
    proposal_id: int | None = Field(description="the value check's proposal")
    deadline: date | None = Field(description="the deadline an overdue test card passed")
    evidence: list[CanvasEvidence] = Field(description="the claim's evidence the caller may read, as it is now")
    hidden_evidence: int = Field(description="pieces of its evidence above what the caller may read")
    proposers: list[str] = Field(description="the members who may not decide it: they changed the status, or opened it")
    decision: Literal[DECISIONS] | None
    decided_by: str | None
    decided_at: datetime | None
    new_deadline: date | None = Field(description="the deadline an extension gave")
    reason: str | None
    rev: int | None = Field(description="the rev the decision wrote")
    decisions: list[str] = Field(description="what the caller may decide of it now; none when they may not")


class CanvasSnapshotClaim(BaseModel):
    """One claim of an approved baseline's snapshot."""

    claim: str
    claim_id: int
    rev: int
    status: Literal[STATUSES] = Field(description="the status in effect")
    sha256: str = Field(description="the SHA-256 of the claim's content at that rev, as canonical JSON")


class CanvasBaselineSummary(BaseModel):
    """A baseline of a canvas."""

    number: int
    kind: Literal[BASELINE_KINDS] = Field(description="weekly: the plugin's job; push: a writer opened it at once")
    week: str | None = Field(description="the ISO week of a weekly baseline, as 2026-W43")
    due_at: datetime | None
    time_zone: str | None
    status: Literal[BASELINE_STATUSES]
    created_by: str | None = Field(description="the writer who opened it at once")
    created_at: datetime
    approved_at: datetime | None
    expired_at: datetime | None
    item_count: int
    waiting: int = Field(description="its items no member has decided yet")


class CanvasBaseline(CanvasBaselineSummary):
    """A baseline whole: its items, and once approved who decided and its snapshot, which never changes."""

    canvas: str
    items: list[CanvasBaselineItem]
    approved_by: list[str]
    snapshot: list[CanvasSnapshotClaim] | None


class CanvasBaselineOpenIn(BaseModel):
    """Open a baseline of one claim at once, for another writer to approve."""

    claim: str = Field(pattern=CODE, description="the claim's code, as CL-3")


class CanvasBaselineDecisionIn(BaseModel):
    """A decision on the item of one claim."""

    decision: Literal[MEMBER_DECISIONS] = Field(
        description="accepted, rejected or deferred for a change of status; extended, retired or deferred for an "
        "overdue test card"
    )
    deadline: date | None = Field(None, description="the new deadline an extension takes, after today")
    reason: str = Field("", max_length=MAX_REASON_CHARS)


def summary_values(row: BaselineRow, found: list[ItemRow]) -> dict:
    return {
        "number": row.number,
        "kind": row.kind,
        "week": row.week,
        "due_at": row.due_at,
        "time_zone": row.time_zone,
        "status": row.status,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "approved_at": row.approved_at,
        "expired_at": row.expired_at,
        "item_count": len(found),
        "waiting": sum(item.waiting for item in found),
    }


def item_model(
    item: ItemRow, evidence: list[EvidenceRow], hidden: int, names: dict[int, str], decisions: list[str]
) -> CanvasBaselineItem:
    return CanvasBaselineItem(
        claim=item.code,
        kind=item.kind,
        from_status=item.from_status,
        at_status=item.at_status,
        to_status=item.to_status,
        claim_rev=item.claim_rev,
        proposal_id=item.proposal_id,
        deadline=item.deadline,
        evidence=[evidence_model(piece) for piece in evidence],
        hidden_evidence=hidden,
        proposers=[names.get(user_id, str(user_id)) for user_id in item.proposers],
        decision=item.decision,
        decided_by=item.decided_by,
        decided_at=item.decided_at,
        new_deadline=item.new_deadline,
        reason=item.reason,
        rev=item.rev,
        decisions=decisions,
    )


__all__ = [
    "CanvasBaseline",
    "CanvasBaselineDecisionIn",
    "CanvasBaselineItem",
    "CanvasBaselineOpenIn",
    "CanvasBaselineSummary",
    "CanvasSnapshotClaim",
    "item_model",
    "summary_values",
]
