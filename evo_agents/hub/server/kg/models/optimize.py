"""The limits of the KG optimize loop (``evo_agents.hub.server.kg_optimize``) and the shape of a run its routes answer
with."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub import kg_optimize as model

LOOP = "kg-optimize"  # the origin of its decisions (kg_review.LOOP)
MAX_TUNE = 300  # tune questions asked at each weight
SESSION_DAYS = 7  # the days of real searches a first run counts
HOLD_REVIEW = timedelta(minutes=30)  # a night's review run waits this long for the first measure of its run
NIGHT_LINK_WAIT = timedelta(hours=12)  # a night's run waits this long for the night's review run
AFTER_WAIT = timedelta(hours=6)  # and this long for a build after the review run, then measures the latest
MAX_DECISIONS = 25  # graph decisions a run applies
MAX_LISTED = 10  # runs GET lists
ERROR_CHARS = 2000
MAX_EXPLORED = 5000  # explored searches a run reads to estimate their reward
NOTICE = "kg_optimize"  # runs.NOTICE_KINDS
KG_OPTIMIZE = "kg.optimize"  # the audit action of an asked-for run


class OptimizeRun(BaseModel):
    id: int
    project: str
    via: Literal["night", "manual"]
    state: Literal["measuring", "running", "after", "done", "reverted", "failed"]
    night: date | None
    agent_run_id: int | None = Field(description="the review run given to the worker on duty")
    before_build_id: int | None
    after_build_id: int | None
    asked_weight: float | None = Field(description="the weight of the asked column the run left in effect")
    summary: str
    report: str | None = Field(description="the report the owner got, markdown; null until the run ends")
    error: str | None
    created_at: datetime
    updated_at: datetime
    ended_at: datetime | None
    body: dict = Field(description="what the run measured, decided and undid")


class OptimizeRuns(BaseModel):
    active: OptimizeRun | None
    runs: list[OptimizeRun]


def shown(row, project: str) -> OptimizeRun:
    body = row.body if isinstance(row.body, dict) else {}
    return OptimizeRun(
        id=row.id,
        project=project,
        via=row.via,
        state=row.state,
        night=row.night,
        agent_run_id=row.agent_run_id,
        before_build_id=row.before_build_id,
        after_build_id=row.after_build_id,
        asked_weight=row.asked_weight,
        summary=model.summary(project, body, row.state),
        report=row.report,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        ended_at=row.ended_at,
        body=body,
    )
