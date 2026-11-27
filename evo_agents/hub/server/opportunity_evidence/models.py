"""The shapes of an opportunity's evidence: a line as a writer adds it or @builder proposes it
(``OpportunityEvidenceWrite``, a problem's line whose signal may also be one of an opportunity's) and as the hub
answers it (``OpportunityEvidence``)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from evo_agents.hub import opportunities
from evo_agents.hub.server.problem_evidence.models import ProblemEvidenceWrite


class OpportunityEvidenceWrite(ProblemEvidenceWrite):
    """One line of evidence of an opportunity, as a writer adds it or @builder proposes it: a problem's line, whose
    signal may also be one of an opportunity's."""

    signal: str = Field(description=f"what it shows: {', '.join(opportunities.SIGNALS)}")

    @field_validator("signal")
    @classmethod
    def _signal(cls, signal: str) -> str:
        if signal not in opportunities.SIGNALS:
            raise ValueError(f"signal is one of {', '.join(opportunities.SIGNALS)}")
        return signal


class OpportunityEvidence(BaseModel):
    """A line of the evidence of an opportunity."""

    project: str
    opportunity: str = Field(description="O-<number>")
    code: str = Field(description="E-<number>, unique in the opportunity")
    number: int
    source_kind: str
    source_uri: str
    locator: str | None
    speaker: str | None
    excerpt: str
    signal: str
    label: str
    added_by: str
    added_at: datetime
    confirmed_by: str | None = Field(description="the writer who confirmed it; null before")
    confirmed_at: datetime | None
    proposal_id: int | None = Field(description="the proposal of @builder that made it; null for a line a writer added")
