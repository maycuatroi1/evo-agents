"""The baselines of a canvas as values: a baseline as the repository reads it (``BaselineRow``), one of its items
(``ItemRow``), an item to propose (``Item``), and what a member decides of each kind of item (``DECIDES``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from evo_agents.hub.server.canvas.models.content import code
from evo_agents.hub.server.canvas.models.rows import ClaimRow

DEFAULT_STATUS = "assumption"  # in effect for a claim no snapshot holds yet
OVERDUE_STATUSES = ("assumption", "testing")  # what a past deadline without real evidence asks a reviewer about
ASKED = ("status", "value_check")  # items that ask for a status
DECIDES = {  # kind of item -> what a member decides of it
    "status": ("accepted", "rejected", "deferred"),
    "value_check": ("accepted", "rejected", "deferred"),
    "overdue": ("extended", "retired", "deferred"),
    "new": (),
}
MAX_LISTED = 100  # baselines one read lists
SELF_APPROVAL = "self_approval"  # the config's field that lets a proposer decide the item too


@dataclass(frozen=True)
class BaselineRow:
    id: int
    canvas_id: int
    number: int
    kind: str
    week: str | None
    due_at: datetime | None
    time_zone: str | None
    status: str
    label: dict
    created_by: str | None
    created_at: datetime
    approved_at: datetime | None
    expired_at: datetime | None
    snapshot: list | None


@dataclass(frozen=True)
class ItemRow:
    id: int
    claim_id: int
    number: int
    kind: str
    from_status: str
    at_status: str
    to_status: str | None
    claim_rev: int
    proposal_id: int | None
    deadline: date | None
    evidence_ids: list[int]
    proposers: list[int]
    label: dict
    decision: str | None
    decided_by: str | None
    decided_at: datetime | None
    new_deadline: date | None
    reason: str | None
    rev: int | None

    @property
    def code(self) -> str:
        return code(self.number)

    @property
    def waiting(self) -> bool:
        """Whether a member has still to decide it."""
        return self.decision is None and bool(DECIDES[self.kind])

    def barred(self, self_approval: bool) -> list[int]:
        """The members who may not decide it: its proposers, or nobody while the config's self_approval is on."""
        return [] if self_approval else list(self.proposers)


@dataclass(frozen=True)
class Item:
    """An item to propose: what it asks of ``claim``."""

    claim: ClaimRow
    kind: str
    from_status: str
    to_status: str | None = None
    proposal_id: int | None = None
    deadline: date | None = None
    evidence_ids: list[int] = field(default_factory=list)
    proposers: list[int] = field(default_factory=list)
