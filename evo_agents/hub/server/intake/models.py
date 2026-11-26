"""The request and response models of the intakes of a project, and their limits."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import Path
from pydantic import BaseModel, Field, StringConstraints, field_validator

from evo_agents.hub import intakes
from evo_agents.hub.server.base.models import StrictModel

IntakeRef = Annotated[str, Path(pattern=intakes.REF, description="I-<number>")]
Said = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
TASKS = "tasks"  # the plugin whose tasks a promise becomes (evo_agents.hub.server.tasks)
MAX_SUMMARY_CHARS = 500  # of a proposal's summary (evo_agents.core_plugins.builder.MAX_SUMMARY_CHARS)
RACE = "another write of the project's tasks or problems took the number first; put the result again"


class IntakeSource(StrictModel):
    """A source the session reads: what it is, never what it says."""

    kind: str = Field(description=f"one of {', '.join(intakes.SOURCE_KINDS)}")
    label: str = Field(description=f"one of {', '.join(intakes.SOURCE_LABELS)}")
    name: Said | None = Field(
        None, max_length=intakes.MAX_SOURCE_NAME_CHARS, description="a short name, such as a group's; never content"
    )

    @field_validator("kind")
    @classmethod
    def _kind(cls, kind: str) -> str:
        if kind not in intakes.SOURCE_KINDS:
            raise ValueError(f"kind is one of {', '.join(intakes.SOURCE_KINDS)}")
        return kind

    @field_validator("label")
    @classmethod
    def _label(cls, label: str) -> str:
        if label not in intakes.SOURCE_LABELS:
            raise ValueError(f"label is one of {', '.join(intakes.SOURCE_LABELS)}")
        return label


class IntakeStart(StrictModel):
    sources: list[IntakeSource] = Field(min_length=1, max_length=intakes.MAX_SOURCES)
    note: Said | None = Field(None, max_length=intakes.MAX_NOTE_CHARS, description="why the member asked for it")


class IntakeProposal(BaseModel):
    candidate: int = Field(description="the candidate's place in the result, from 1")
    kind: Literal["new_problem", "evidence"]
    problem: str = Field(description="P-<number>: the code a new problem would take, or the problem the lines join")
    proposal_id: int = Field(description="the pending proposal of @builder a writer approves")
    lines: int


class IntakeOpportunityProposal(BaseModel):
    candidate: int = Field(description="the candidate opportunity's place in the result, from 1")
    kind: Literal["new_opportunity", "evidence"]
    opportunity: str = Field(description="O-<number>: the code a new opportunity would take, or the one the lines join")
    proposal_id: int = Field(description="the pending proposal of @builder a writer approves")
    lines: int


class IntakeTask(BaseModel):
    promise: int = Field(description="the promise's place in the result, from 1")
    task: str = Field(description="the task's code, such as T-12")
    assignee: str
    due_on: date | None
    delivery_seen: bool


class IntakeOutcome(BaseModel):
    """What the hub made of a result."""

    proposals: list[IntakeProposal]
    tasks: list[IntakeTask]
    opportunity_proposals: list[IntakeOpportunityProposal] = Field(
        default_factory=list, description="the proposals made for the candidate opportunities of an evo-intake/2 result"
    )
    tasks_skipped: str | None = Field(
        description="why the promises made no task (the plugin tasks is not enabled in the project); null otherwise"
    )
    questions: list[dict] = Field(description="the questions the session could not answer, as it put them")


class Intake(BaseModel):
    """An intake of a project."""

    project: str
    code: str = Field(description="I-<number>, unique in the project")
    number: int
    status: Literal[intakes.STATUSES]  # pyright: ignore[reportInvalidTypeForm]
    requested_by: str
    sources: list[IntakeSource]
    note: str | None
    created_at: datetime
    put_by: str | None = Field(description="who put the result; null while it is open")
    done_at: datetime | None
    candidates: int | None = Field(description="how many candidates the result held; null while it is open")
    promises: int | None
    questions: int | None
    opportunities: int | None = Field(
        None, description="how many candidate opportunities the result held (evo-intake/2); null while it is open"
    )
    outcome: IntakeOutcome | None = Field(description="what the hub made of the result; null while it is open")
