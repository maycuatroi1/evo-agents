"""The request and response models of the opportunities of a project, their limits, and the problems a revision
answers (``Answer``)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal, NamedTuple

from fastapi import Path
from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from evo_agents.hub import opportunities
from evo_agents.hub.server.base.models import LOGIN_NAME, StrictModel
from evo_agents.hub.server.problems.models import ProblemMetric

MAX_REVISION = 2**31 - 1
OpportunityRef = Annotated[str, Path(pattern=opportunities.REF, description="O-<number>")]
AnswerCode = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        max_length=120,
        pattern=r"^(?:[a-z0-9][a-z0-9-]{0,99}/)?P-[1-9][0-9]{0,8}$",
    ),
]


class ProjectOpportunity(BaseModel):
    """An opportunity of a project at one revision."""

    project: str
    code: str = Field(description="O-<number>, unique in the project and never given again")
    number: int
    revision: int
    statement: str = Field(description="what the project could win")
    beneficiary: str = Field(description="who benefits")
    leverage: str = Field(description="what makes it reachable now")
    window_until: date | None = Field(description="the date after which it is gone; null: none known")
    window_note: str | None = Field(description="what the window is")
    answers: list[str] = Field(
        description="the problems it answers that the reader sees: P-<n>, or <project>/P-<n> for another project's"
    )
    hidden_answers: int = Field(
        description="the problems of other projects it answers that the reader holds no grant on"
    )
    metric: ProblemMetric | None = Field(description="null: no metric")
    rank: int = Field(description="its place among the project's opportunities, 1 first")
    owner: str
    status: Literal[opportunities.STATUSES]  # pyright: ignore[reportInvalidTypeForm]
    close_reason: str | None = Field(description="why it was captured, missed or dropped; null while it is open")
    note: str | None = Field(description="what the writer said of this revision")
    created_by: str = Field(description="who wrote this revision")
    created_at: datetime


class ProjectOpportunityShown(ProjectOpportunity):
    evidence_count: int = Field(description="the lines of evidence of the opportunity")
    source_count: int = Field(
        description="the independent sources those lines come from: a source is a kind and an address"
    )


class ProjectOpportunityWritten(ProjectOpportunity):
    changed: bool = Field(description="false when the newest revision is this opportunity already: nothing was written")


def _said_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("says something")
    return value


class ProjectOpportunityWrite(StrictModel):
    """The next revision of an opportunity: the whole of it, as its writer wants it."""

    statement: str = Field(min_length=1, max_length=opportunities.MAX_STATEMENT_CHARS, description="what could be won")
    beneficiary: str = Field(min_length=1, max_length=opportunities.MAX_TEXT_CHARS, description="who benefits")
    leverage: str = Field(
        min_length=1, max_length=opportunities.MAX_TEXT_CHARS, description="what makes it reachable now"
    )
    window_until: date | None = Field(None, description="the date after which it is gone")
    window_note: str | None = Field(None, min_length=1, max_length=opportunities.MAX_TEXT_CHARS)
    answers: list[AnswerCode] = Field(
        default_factory=list,
        max_length=opportunities.MAX_ANSWERS,
        description="the problems it answers: P-3, or <project>/P-3 for another project's; the whole list",
    )
    metric: ProblemMetric | None = None
    rank: int | None = Field(
        None,
        ge=1,
        le=opportunities.MAX_RANK,
        description="null: kept, or after the project's open opportunities for a new one",
    )
    owner: str | None = Field(
        None,
        pattern=LOGIN_NAME,
        description="a member with the writer role or above; null: kept, or the writer for a new opportunity",
    )
    status: Literal[opportunities.STATUSES] = opportunities.OPEN  # pyright: ignore[reportInvalidTypeForm]
    close_reason: str | None = Field(
        None,
        min_length=1,
        max_length=opportunities.MAX_REASON_CHARS,
        description="why it was captured, missed or dropped, for a closed opportunity and only for one",
    )
    note: str | None = Field(None, max_length=opportunities.MAX_NOTE_CHARS, description="why, for the history")
    if_revision: int | None = Field(
        None, ge=0, description="409 unless the opportunity is at this revision now; 0: new only"
    )

    @field_validator(*opportunities.TEXTS)
    @classmethod
    def _said(cls, text: str) -> str:
        return _said_text(text)

    @field_validator("window_note")
    @classmethod
    def _note_said(cls, text: str | None) -> str | None:
        return None if text is None else _said_text(text)

    @field_validator("close_reason")
    @classmethod
    def _reason_said(cls, reason: str | None) -> str | None:
        return None if reason is None else _said_text(" ".join(reason.split()))

    @model_validator(mode="after")
    def _reason_of_closed(self):
        if (self.status != opportunities.OPEN) != (self.close_reason is not None):
            raise ValueError("a closed opportunity has a close_reason, and an open one has none")
        return self


class ProjectOpportunityClose(StrictModel):
    status: Literal[opportunities.CLOSED] = Field(description="captured, missed or dropped")  # pyright: ignore[reportInvalidTypeForm]
    reason: str = Field(min_length=1, max_length=opportunities.MAX_REASON_CHARS, description="why")
    note: str | None = Field(None, max_length=opportunities.MAX_NOTE_CHARS, description="for the history")
    if_revision: int | None = Field(None, ge=1, description="409 unless the opportunity is at this revision now")

    @field_validator("reason")
    @classmethod
    def _said(cls, reason: str) -> str:
        return _said_text(" ".join(reason.split()))


class OpportunityRevision(BaseModel):
    revision: int
    status: Literal[opportunities.STATUSES]  # pyright: ignore[reportInvalidTypeForm]
    note: str | None
    created_by: str
    created_at: datetime
    changed: list[str] = Field(description="the fields whose value differs from the revision before")


class Answer(NamedTuple):
    """A problem a revision answers: where it lives and its number."""

    problem_id: int
    project_id: int
    project: str
    number: int

    def text(self, own: int) -> str:
        return opportunities.answer_text(None if self.project_id == own else self.project, self.number)
