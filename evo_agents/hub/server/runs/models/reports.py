"""What a worker reports of a run (its state and its steps), the listing and stats of runs, and a plan run's plan."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evo_agents.hub import proofs, question_runs, runs
from evo_agents.hub.server.runs.models.limits import (
    LINE,
    MAX_REPORT_BYTES,
    MAX_STEP_EVIDENCE_CHARS,
    OBJECT_NAME,
    STEP_KEY_CHARS,
    ModelName,
    StepReportStatus,
    json_bytes,
)
from evo_agents.hub.server.runs.models.routing import RoutingRecord


class VerifyResult(BaseModel):
    command: str = Field(min_length=1, max_length=2000)
    exit_code: int
    duration_ms: int | None = Field(None, ge=0)


class Diffstat(BaseModel):
    files: int = Field(ge=0)
    insertions: int = Field(ge=0)
    deletions: int = Field(ge=0)


class StepProof(BaseModel):
    """The proof of one criterion a step names (``evo_agents.hub.proofs``)."""

    id: str = Field(pattern=r"^a\d+$", max_length=20, description="the criterion's id in the plan's acceptance")
    kind: proofs.Kind = Field(description="test (path::name), command, or n/a")
    locator: str | None = Field(
        None, min_length=1, max_length=proofs.MAX_PROOF_CHARS, pattern=LINE, description="the test or the command"
    )
    reason: str | None = Field(
        None, min_length=1, max_length=proofs.MAX_PROOF_CHARS, pattern=LINE, description="with n/a: why"
    )

    @model_validator(mode="after")
    def _kind_has_its_key(self):
        if self.kind == proofs.NA and not self.reason:
            raise ValueError(f"the proof of {self.id} is n/a, which comes with a reason")
        if self.kind != proofs.NA and not self.locator:
            raise ValueError(f"the proof of {self.id} is a {self.kind}, which comes with its locator")
        return self


class StateReport(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    state: runs.RunState = Field(description="the state the run moves to")
    from_state: runs.RunState | None = Field(
        None, alias="from", description="the state the worker believes the run is in; 409 when it is in another"
    )
    session_id: str | None = Field(None, min_length=1, max_length=200, pattern=LINE)
    commit_sha: str | None = Field(None, pattern=OBJECT_NAME)
    diffstat: Diffstat | None = None
    verify: list[VerifyResult] | None = Field(None, max_length=50, description="each verify command the daemon ran")
    usage: dict | None = None
    error: str | None = Field(None, min_length=1, max_length=2000)
    failure_cause: str | None = Field(
        None,
        pattern=runs.FAILURE_CAUSE,
        description="with failed: why, one of runs.FAILURE_CAUSES (a newer worker's own names are kept too); kept with "
        "the run as its failure_cause, and ignored with any other state",
    )
    summary: str | None = Field(None, max_length=8000, description="the agent's summary, for the evidence")
    model_used: ModelName | None = Field(None, description="the model the run's runtime runs it on")
    routing: RoutingRecord | None = Field(None, description="what the routing by difficulty did, for a run it rated")
    proofs: list[StepProof] | None = Field(
        None,
        max_length=proofs.MAX_PROOFS,
        description="with done or review of a run of one step: the proof of each criterion it names, which the "
        "step keeps once done",
    )
    answer: str | None = Field(
        None,
        min_length=1,
        max_length=question_runs.MAX_ANSWER_CHARS,
        description="with done of a question run, and only then (422 otherwise): the agent's answer, which goes to "
        "the core plugin that asked",
    )

    @model_validator(mode="after")
    def _bounded(self):
        if self.usage is not None and json_bytes(self.usage) > MAX_REPORT_BYTES:
            raise ValueError(f"usage takes at most {MAX_REPORT_BYTES} bytes of JSON")
        return self


class StepReport(BaseModel):
    """What a plan run's worker reports of one step of the plan."""

    status: StepReportStatus = Field(
        description="in_progress when the agent starts the step, done once verified, pending to hand it back"
    )
    repo: str | None = Field(
        None, min_length=1, max_length=STEP_KEY_CHARS, pattern=LINE, description="the run's repo the step was done in"
    )
    evidence: str | None = Field(
        None, min_length=1, max_length=MAX_STEP_EVIDENCE_CHARS, description="what the agent did and how it checked it"
    )
    verify: list[VerifyResult] | None = Field(
        None, max_length=50, description="each verify command the worker ran again; done needs one, every one exit 0"
    )
    commit_sha: str | None = Field(None, pattern=OBJECT_NAME, description="the repo's commit the step ends on")
    proofs: list[StepProof] | None = Field(
        None,
        max_length=proofs.MAX_PROOFS,
        description="with done: one proof for each criterion of the plan's acceptance the step names, which the step "
        "keeps; 422 when one is missing or names another criterion. Absent from an older worker's report",
    )


class StepWritten(BaseModel):
    run_id: int
    plan_id: str
    step_key: str
    status: str | None = Field(description="the step's status as the plan holds it after the report")
    revision: int = Field(description="the plan's revision after the report")
    written: bool = Field(description="whether the report wrote a revision; false for a resend that changed nothing")


# Listing runs and their stats


class RunFigures(BaseModel):
    """The runs that ended in a span of UTC days, of the plans the caller may read."""

    done: int = Field(description="runs that ended done")
    failed: int = Field(description="runs that ended failed")
    lost: int = Field(description="runs whose worker stopped extending the lease")
    cancelled: int = Field(description="runs that ended cancelled, queued or not")
    p50_seconds: float | None = Field(
        description="the median time the runs that ended ran, from started (else leased) to finished, as the runs "
        "pages count it; null when none of them started"
    )
    p90_seconds: float | None = Field(description="the 90th percentile of that time; null when none of them started")
    input_tokens: int = Field(
        description="input tokens not read from the cache, added up from the runs' usage as the run page's usage "
        "card reads it"
    )
    output_tokens: int = Field(description="output tokens, reasoning left out")
    cache_read_tokens: int = Field(description="input tokens read from the cache")
    reasoning_tokens: int = Field(description="reasoning (thinking) tokens")
    runs_with_usage: int = Field(
        description="the runs whose usage the hub could read: a run without usage, or with usage of no shape the "
        "card knows (Claude Code, Codex, opencode), adds no token"
    )


class RunDay(RunFigures):
    day: date = Field(description="a day in UTC")


class RunStats(BaseModel):
    project: str
    days: int = Field(description="the days counted, today in UTC the last")
    first_day: date = Field(description="the oldest day counted, in UTC")
    last_day: date = Field(description="today in UTC")
    by_day: list[RunDay] = Field(
        description="every day counted, the oldest first: a day without a run has zeros and null percentiles"
    )
    total: RunFigures = Field(
        description="the whole span: its counts and tokens are by_day's added up, its percentiles over every run"
    )


# A plan run's plan


class AuthoredPlan(BaseModel):
    body: dict[str, Any] = Field(description="the plan as its YAML file reads; a hub key in it is ignored")
    if_revision: int | None = Field(
        None, ge=0, description="the revision being replaced; leave it out, or 0, to create the plan"
    )
    label: dict | None = Field(None, description="{level, location, integrity}; default: the project's, or the held")
