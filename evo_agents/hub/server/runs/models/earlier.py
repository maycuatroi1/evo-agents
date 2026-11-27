"""The shapes and limits of what earlier review nights found (``evo_agents.hub.server.earlier``): a finding with what
became of it, the proposals that name it, and the proposals rejected lately."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub import tiers

STATUSES = ("open", "proposed", "rejected", "merged", "kept", "reverted")
CLOSED = ("rejected", "merged", "kept")
PRECEDENCE = ("kept", "merged", "proposed", "reverted", "rejected")  # the status a finding takes from its proposals
PER_LENS = 5  # findings of one lens in the prompt
REJECTED_SHOWN = 10  # rejected proposals in the prompt
SIMILAR = 0.5  # of the words of two titles, for a finding to be like another
SCAN = 2000  # findings one read examines
MAX_LIMIT = 100
REPEATED = (
    "finding #{id} ({lens}, {status}) of an earlier night found this and is closed, and it cited every piece of "
    "evidence of this one: cite evidence it did not, or leave it; nothing was recorded"
)


class EarlierProposal(BaseModel):
    id: int
    kind: str
    tier: int
    state: str = Field(description="the proposal's state")
    change_state: str | None = Field(description="the state of the change accepting it made, if any")
    outcome: str | None = Field(description="the ledger's outcome of its merged change: keep, revert or unclear")
    status: Literal[STATUSES] = Field(description="what it says of the findings it names")  # pyright: ignore[reportInvalidTypeForm]
    note: str | None = Field(description="the owner's note with the answer")


class EarlierFinding(BaseModel):
    id: int
    run_id: int
    night: date | None = Field(description="the night of the review run that found it")
    lens: str
    severity: str
    title: str
    status: Literal[STATUSES]  # pyright: ignore[reportInvalidTypeForm]
    closed: bool = Field(description="kept, merged or rejected: not recorded again without new evidence")
    proposals: list[EarlierProposal]
    evidence: list[str] = Field(description="its evidence, as the command line writes it")


class RejectedProposal(BaseModel):
    id: int
    lens: str
    kind: str
    title: str
    note: str | None
    answered_at: datetime | None


class EarlierView(BaseModel):
    project: str
    findings: list[EarlierFinding] = Field(description="newest first")
    rejected: list[RejectedProposal] = Field(
        description=f"rejected in the last {tiers.REJECTED_DAYS} days, newest first"
    )
