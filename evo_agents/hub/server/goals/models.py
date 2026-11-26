"""The request and response models of the goals of a project, and their limits."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import Path
from pydantic import BaseModel, Field, field_validator, model_validator

from evo_agents.hub import goals
from evo_agents.hub import opportunities as opportunity_model
from evo_agents.hub import problems as problem_model
from evo_agents.hub.server.base.models import LOGIN_NAME, StrictModel
from evo_agents.hub.srs_backlog import BACKLOG_METRIC
from evo_agents.hub.tables_canvas import SLUG
from evo_agents.hub.tables_signals import KEY_NAME, MAX_KEY_NAME_CHARS
from evo_agents.hub.tables_srs import MAX_ITEM_TITLE_CHARS
from evo_agents.hub.tables_srs import SLUG as SRS_SLUG

MAX_REVISION = 2**31 - 1
NUMBER_KIND = "number"  # tables_signals.KINDS: the kind of key a metric names
# goals.FIELDS whose change makes the open proposals of the Loop's brainstorms of the goal stale
BRAINSTORMED_FOR = ("statement", "metric")
ALIAS = re.compile(goals.ALIAS)
GoalRef = Annotated[
    str, Path(pattern=goals.REF, description="G-<number>, or the alias of a goal made from the charter")
]


class GoalMetric(StrictModel):
    """How to tell a goal is reached: a key of the project's signals summed up over a window, compared with a target,
    as a test card of the plugin canvas compares one."""

    key: str = Field(
        min_length=1,
        max_length=MAX_KEY_NAME_CHARS,
        pattern=KEY_NAME,
        description="a key of kind number of the project's registry of signals",
    )
    aggregate: Literal[goals.AGGREGATES]  # pyright: ignore[reportInvalidTypeForm]
    window_days: int | None = Field(
        None, ge=1, le=goals.MAX_WINDOW_DAYS, description=f"null: {goals.DEFAULT_WINDOW_DAYS}"
    )
    comparator: Literal[goals.COMPARATORS]  # pyright: ignore[reportInvalidTypeForm]
    target: float = Field(allow_inf_nan=False)
    tenant: str | None = Field(None, min_length=1, max_length=goals.MAX_TENANT_CHARS, description="one tenant's alone")


class ProjectGoal(BaseModel):
    """A goal of a project at one revision."""

    project: str
    code: str = Field(description="G-<number>, unique in the project and never given again")
    number: int
    alias: str | None = Field(description="the id of the charter's goal the hub made it from, which names it too")
    revision: int
    statement: str
    metric: GoalMetric | None = Field(description="null: no metric, so the Curator reads it and nothing else is done")
    deadline: date | None
    canvas: str | None = Field(description="the slug of the canvas whose product it drives")
    problem: str | None = Field(
        description="the code (P-<number>) of the problem of the project it answers; null: none"
    )
    opportunity: str | None = Field(
        default=None,
        description="the code (O-<number>) of the opportunity of the project it answers; null: none. A goal answers "
        "a problem or an opportunity, never both",
    )
    srs_document: str | None = Field(
        default=None,
        description="the slug of the SRS document whose approved use cases the goal delivers; null: none",
    )
    srs_heading: str | None = Field(
        default=None, description="the code or title of a heading of that document the goal is limited to; null: all"
    )
    rank: int = Field(description="its place among the project's goals, 1 first")
    owner: str
    status: Literal[goals.STATUSES]  # pyright: ignore[reportInvalidTypeForm]
    close_reason: Literal[goals.CLOSE_REASONS] | None = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="why it is closed; null unless it is"
    )
    note: str | None = Field(description="what the writer said of this revision")
    created_by: str = Field(description="who wrote this revision")
    created_at: datetime


class ProjectGoalWritten(ProjectGoal):
    changed: bool = Field(description="false when the newest revision is this goal already: nothing was written")


class ProjectGoalWrite(StrictModel):
    """The next revision of a goal: the whole of it, as its writer wants it."""

    statement: str = Field(min_length=1, max_length=goals.MAX_STATEMENT_CHARS)
    metric: GoalMetric | None = None
    deadline: date | None = None
    canvas: str | None = Field(None, pattern=SLUG, description="the slug of a canvas of the project")
    problem: str | None = Field(
        None,
        pattern=problem_model.REF,
        description="the code (P-<number>) of a problem of the project that is not dropped; null: none",
    )
    opportunity: str | None = Field(
        None,
        pattern=opportunity_model.REF,
        description="the code (O-<number>) of an opportunity of the project that is not dropped; null: none. Not "
        "with a problem",
    )
    srs_document: str | None = Field(
        None,
        pattern=SRS_SLUG,
        description="the slug of a document of the project, not archived, whose approved use cases the goal delivers; "
        f"null: none. The metric {BACKLOG_METRIC} needs one",
    )
    srs_heading: str | None = Field(
        None,
        min_length=1,
        max_length=MAX_ITEM_TITLE_CHARS,
        description="the code or title of a heading of that document to limit the goal to; null: all. Not without a "
        "document",
    )
    rank: int | None = Field(
        None, ge=1, le=goals.MAX_RANK, description="null: kept, or after the project's other goals for a new one"
    )
    owner: str | None = Field(
        None,
        pattern=LOGIN_NAME,
        description="a member with the writer role or above; null: kept, or the writer for a new goal",
    )
    status: Literal[goals.STATUSES] = goals.ACTIVE  # pyright: ignore[reportInvalidTypeForm]
    close_reason: Literal[goals.CLOSE_REASONS] | None = Field(  # pyright: ignore[reportInvalidTypeForm]
        None, description="for a closed goal, and only for one"
    )
    note: str | None = Field(None, max_length=goals.MAX_NOTE_CHARS, description="why, for the history")
    if_revision: int | None = Field(None, ge=0, description="409 unless the goal is at this revision now; 0: new only")

    @field_validator("statement")
    @classmethod
    def _said(cls, statement: str) -> str:
        statement = statement.strip()
        if not statement:
            raise ValueError("the statement says something")
        return statement

    @model_validator(mode="after")
    def _reason_of_closed(self):
        if (self.status == goals.CLOSED) != (self.close_reason is not None):
            raise ValueError("a closed goal has a close_reason, and a goal that is not closed has none")
        return self


class ProjectGoalClose(StrictModel):
    reason: Literal[goals.CLOSE_REASONS]  # pyright: ignore[reportInvalidTypeForm]
    note: str | None = Field(None, max_length=goals.MAX_NOTE_CHARS, description="why, for the history")
    if_revision: int | None = Field(None, ge=1, description="409 unless the goal is at this revision now")


class GoalRevision(BaseModel):
    revision: int
    status: Literal[goals.STATUSES]  # pyright: ignore[reportInvalidTypeForm]
    note: str | None
    created_by: str
    created_at: datetime
    changed: list[str] = Field(description="the fields whose value differs from the revision before")
