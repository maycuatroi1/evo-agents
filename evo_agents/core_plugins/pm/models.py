"""The API of the plugin pm as Pydantic models: the daily report and one member's view of it. Every model is named
``Pm...`` in the hub's OpenAPI document. The keys of ``PmDaily`` are the contract a project's harness reads.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class PmTask(BaseModel):
    ref: str = Field(description="the task's number as shown, T-42")
    title: str
    assignees: list[str]


class PmDoneTask(PmTask):
    done_at: datetime = Field(description="when it moved to a status of group done, inside the window")


class PmDoneUseCase(BaseModel):
    code: str
    title: str
    task: str = Field(description="the task whose state is the use case's: its frame task, else the task opened last")
    done_at: datetime
    outcome: str | None = Field(
        description="its outcome_md, else the section `## Kết quả` or `## Outcome` that ends its description"
    )


class PmSprintUseCase(BaseModel):
    code: str
    title: str
    done: int
    total: int = Field(description="the tasks of the sprint that link it, not cancelled")


class PmSprint(BaseModel):
    name: str
    starts_on: date | None
    ends_on: date | None
    opened_at: datetime = Field(description="when it turned active, else its first day, else when it was made")
    scope: int = Field(description="the tasks it held when it opened")
    done_percent: float | None = Field(description="of that scope, done now; cancelled tasks leave the whole")
    added: int = Field(description="tasks in it now that were not in it when it opened")
    removed: int = Field(description="tasks that were in it when it opened and are not now")
    working_days: int | None = Field(description="in the sprint, from its opening to ends_on")
    working_days_gone: int | None = Field(description="before the date of the report")
    time_percent: float | None = Field(description="working_days_gone over working_days")
    use_cases: list[PmSprintUseCase]


class PmLate(PmTask):
    due_on: date
    kind: Literal["overdue", "at_risk"]
    days_late: int | None = Field(description="working days past due_on; overdue only")
    days_left: int | None = Field(description="working days left to due_on, today's included; at risk only")


class PmReview(PmTask):
    qa: list[str]
    since: datetime = Field(description="when it moved to a status of group review")
    age_days: int = Field(description="working days since")


class PmStale(PmTask):
    last_activity_at: datetime
    source: str = Field(description="history, comment, run, plan_step or created")
    idle_days: int


class PmBlocked(BaseModel):
    ref: str
    title: str
    kind: Literal["manual", "decision"]
    reason: str
    waiting_kind: Literal["member", "task", "text"]
    waiting_on: str
    since: datetime
    set_by: str | None
    age_days: int = Field(description="working days since")
    decision_id: int | None
    run_id: int | None


class PmEscalate(PmBlocked):
    escalate_to: str | None = Field(description="the config's pm: whom the escalation is for")


class PmLoad(BaseModel):
    login: str
    role: str
    doing: int
    wip_limit: int
    overloaded: bool = Field(description="doing is above wip_limit")
    empty: bool = Field(description="no task doing")
    review_as_qa: int
    late: int
    doing_tasks: list[str]


class PmHidden(BaseModel):
    done_use_cases: int = 0
    done_tasks: int = 0
    sprint: int = 0
    late: int = 0
    review: int = 0
    stale: int = 0
    blocked: int = 0
    escalate: int = 0


class PmDaily(BaseModel):
    """What the project did on the working day before ``date``, and what needs the PM today."""

    project: str
    date: date
    previous_working_day: date
    done_use_cases: list[PmDoneUseCase]
    done_tasks: list[PmDoneTask]
    sprint: PmSprint | None
    late: list[PmLate]
    review: list[PmReview]
    stale: list[PmStale]
    blocked: list[PmBlocked]
    escalate: list[PmEscalate]
    load: list[PmLoad]
    hidden: PmHidden = Field(description="what each list leaves out because the reader may not see it")


class PmMemberHidden(BaseModel):
    doing: int = 0
    review: int = 0
    late: int = 0
    blocked: int = 0
    blocking: int = 0


class PmMember(BaseModel):
    """One member's day: the tasks they do and review, those late, the blockers on their tasks and those that wait
    on them."""

    project: str
    date: date
    login: str
    doing: list[PmTask]
    review: list[PmReview]
    late: list[PmLate]
    blocked: list[PmBlocked]
    blocking: list[PmBlocked] = Field(description="blockers, on any task, that wait on this member")
    hidden: PmMemberHidden


__all__ = ["PmDaily", "PmMember"]
