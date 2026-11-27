"""The shapes of a problem's evidence: a line as a writer adds it or @builder proposes it (``ProblemEvidenceWrite``,
which refuses a personal label and an excerpt longer than 300 characters with 422) and as the hub answers it
(``ProblemEvidence``), and the path of a line (``EvidenceRef``, E-<number>)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import Path
from pydantic import BaseModel, Field, StringConstraints, field_validator

from evo_agents.hub import problems
from evo_agents.hub.server.base.models import StrictModel

EvidenceRef = Annotated[str, Path(pattern=problems.EVIDENCE_REF, description="E-<number>")]
Said = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ProblemEvidenceWrite(StrictModel):
    """One line of evidence, as a writer adds it or @builder proposes it."""

    source_kind: str = Field(description=f"where it was read: {', '.join(problems.SOURCE_KINDS)}")
    source_uri: Said = Field(max_length=problems.MAX_URI_CHARS, description="the source's URI or path")
    locator: Said | None = Field(
        None, max_length=problems.MAX_LOCATOR_CHARS, description="where in the source: a message id, a time, a page"
    )
    speaker: Said | None = Field(None, max_length=problems.MAX_SPEAKER_CHARS, description="who spoke, when someone did")
    excerpt: Said = Field(max_length=problems.MAX_EXCERPT_CHARS, description="at most 300 characters of the source")
    signal: str = Field(description=f"what it shows: {', '.join(problems.SIGNALS)}")
    label: str = Field(description="public or internal; a personal line stays on the member's machine")

    @field_validator("source_kind")
    @classmethod
    def _kind(cls, kind: str) -> str:
        if kind not in problems.SOURCE_KINDS:
            raise ValueError(f"source_kind is one of {', '.join(problems.SOURCE_KINDS)}")
        return kind

    @field_validator("signal")
    @classmethod
    def _signal(cls, signal: str) -> str:
        if signal not in problems.SIGNALS:
            raise ValueError(f"signal is one of {', '.join(problems.SIGNALS)}")
        return signal

    @field_validator("label")
    @classmethod
    def _label(cls, label: str) -> str:
        if label == "personal":
            raise ValueError("a personal line stays on the member's machine: label it public or internal, or keep it")
        if label not in problems.EVIDENCE_LABELS:
            raise ValueError(f"label is one of {', '.join(problems.EVIDENCE_LABELS)}")
        return label

    def same(self) -> tuple:
        """What makes a line the same as another: its source, the place in it and the excerpt."""
        return (self.source_kind, self.source_uri, self.locator, self.excerpt)


class ProblemEvidence(BaseModel):
    """A line of the evidence of a problem."""

    project: str
    problem: str = Field(description="P-<number>")
    code: str = Field(description="E-<number>, unique in the problem")
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
