"""The request and response models of the problems of a project, and their limits."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import Path
from pydantic import BaseModel, Field, field_validator, model_validator

from evo_agents.hub import goals, problems
from evo_agents.hub.plans import AREAS
from evo_agents.hub.server.base.models import LOGIN_NAME, StrictModel
from evo_agents.hub.tables_signals import KEY_NAME, MAX_KEY_NAME_CHARS

MAX_REVISION = 2**31 - 1
ProblemRef = Annotated[str, Path(pattern=problems.REF, description="P-<number>")]


class ProblemMetric(StrictModel):
    """How much a problem hurts: a key of the project's signals summed up over a window, as a goal's metric is, with
    no target."""

    key: str = Field(
        min_length=1,
        max_length=MAX_KEY_NAME_CHARS,
        pattern=KEY_NAME,
        description="a key of kind number of the project's registry of signals",
    )
    aggregate: Literal[problems.AGGREGATES]  # pyright: ignore[reportInvalidTypeForm]
    window_days: int | None = Field(
        None, ge=1, le=problems.MAX_WINDOW_DAYS, description=f"null: {problems.DEFAULT_WINDOW_DAYS}"
    )
    tenant: str | None = Field(
        None, min_length=1, max_length=problems.MAX_TENANT_CHARS, description="one tenant's alone"
    )


class ProblemGoal(BaseModel):
    """A goal of the project whose newest revision answers the problem."""

    code: str = Field(description="G-<number>")
    revision: int
    statement: str
    status: Literal[goals.STATUSES]  # pyright: ignore[reportInvalidTypeForm]


class ProjectProblem(BaseModel):
    """A problem of a project at one revision."""

    project: str
    code: str = Field(description="P-<number>, unique in the project and never given again")
    number: int
    revision: int
    statement: str = Field(description="what hurts, before any solution")
    who: str = Field(description="who is hurt")
    when: str = Field(description="when it happens")
    workaround: str = Field(description="how they cope today")
    metric: ProblemMetric | None = Field(description="null: no metric")
    rank: int = Field(description="its place among the project's problems, 1 first")
    owner: str
    status: Literal[problems.STATUSES]  # pyright: ignore[reportInvalidTypeForm]
    close_reason: str | None = Field(description="why it was solved or dropped; null while it is open")
    note: str | None = Field(description="what the writer said of this revision")
    created_by: str = Field(description="who wrote this revision")
    created_at: datetime


class ProblemClaim(BaseModel):
    """A claim of a canvas of the project that links the problem: not archived, in a canvas that is not."""

    canvas: str = Field(description="the canvas's slug")
    canvas_name: str = Field(description="the product's name")
    code: str = Field(description="CL-<number>")
    cell: str
    status: str
    confidence: str
    text: str


class ProblemPlan(BaseModel):
    """A plan of the project whose body names the problem in its problems."""

    plan_id: str
    area: Literal[AREAS]  # pyright: ignore[reportInvalidTypeForm]
    title: str | None
    status: str | None = Field(description="the status the plan's body gives, if any")
    revision: int
    steps_total: int
    steps_done: int = Field(description="its steps done or descoped")


class ProjectProblemShown(ProjectProblem):
    evidence_count: int = Field(description="the lines of evidence of the problem (plan problem-intake)")
    source_count: int = Field(
        description="the independent sources those lines come from: a source is a kind and an address"
    )
    goals: list[ProblemGoal] = Field(description="the goals whose newest revision answers it, by code")
    claims: list[ProblemClaim] = Field(description="the claims that link it, by canvas and code: those the member sees")
    plans: list[ProblemPlan] = Field(description="the plans that name it, by plan id: those the member sees")


class ProjectProblemWritten(ProjectProblem):
    changed: bool = Field(description="false when the newest revision is this problem already: nothing was written")


def _said_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("says something")
    return value


class ProjectProblemWrite(StrictModel):
    """The next revision of a problem: the whole of it, as its writer wants it."""

    statement: str = Field(min_length=1, max_length=problems.MAX_STATEMENT_CHARS, description="what hurts")
    who: str = Field(min_length=1, max_length=problems.MAX_TEXT_CHARS, description="who is hurt")
    when: str = Field(min_length=1, max_length=problems.MAX_TEXT_CHARS, description="when it happens")
    workaround: str = Field(min_length=1, max_length=problems.MAX_TEXT_CHARS, description="how they cope today")
    metric: ProblemMetric | None = None
    rank: int | None = Field(
        None, ge=1, le=problems.MAX_RANK, description="null: kept, or after the project's open problems for a new one"
    )
    owner: str | None = Field(
        None,
        pattern=LOGIN_NAME,
        description="a member with the writer role or above; null: kept, or the writer for a new problem",
    )
    status: Literal[problems.STATUSES] = problems.OPEN  # pyright: ignore[reportInvalidTypeForm]
    close_reason: str | None = Field(
        None,
        min_length=1,
        max_length=problems.MAX_REASON_CHARS,
        description="why it was solved or dropped, for a closed problem and only for one",
    )
    note: str | None = Field(None, max_length=problems.MAX_NOTE_CHARS, description="why, for the history")
    if_revision: int | None = Field(
        None, ge=0, description="409 unless the problem is at this revision now; 0: new only"
    )

    @field_validator(*problems.TEXTS)
    @classmethod
    def _said(cls, text: str) -> str:
        return _said_text(text)

    @field_validator("close_reason")
    @classmethod
    def _reason_said(cls, reason: str | None) -> str | None:
        return None if reason is None else _said_text(" ".join(reason.split()))

    @model_validator(mode="after")
    def _reason_of_closed(self):
        if (self.status != problems.OPEN) != (self.close_reason is not None):
            raise ValueError("a solved or dropped problem has a close_reason, and an open one has none")
        return self


class ProjectProblemClose(StrictModel):
    status: Literal[problems.CLOSED] = Field(description="solved or dropped")  # pyright: ignore[reportInvalidTypeForm]
    reason: str = Field(min_length=1, max_length=problems.MAX_REASON_CHARS, description="why")
    note: str | None = Field(None, max_length=problems.MAX_NOTE_CHARS, description="for the history")
    if_revision: int | None = Field(None, ge=1, description="409 unless the problem is at this revision now")

    @field_validator("reason")
    @classmethod
    def _said(cls, reason: str) -> str:
        return _said_text(" ".join(reason.split()))


class ProblemRevision(BaseModel):
    revision: int
    status: Literal[problems.STATUSES]  # pyright: ignore[reportInvalidTypeForm]
    note: str | None
    created_by: str
    created_at: datetime
    changed: list[str] = Field(description="the fields whose value differs from the revision before")
