"""The shapes of the probe runs of the Curator (``evo_agents.hub.server.probes.service.runs`` queues and ends them): a
session's result a worker reports (``ProbeReport``, ``ProbeSession``), a row of curator_probe_runs (``ProbeRun``) and
what an admin's requeue did (``ProbeRequeue``)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evo_agents.hub import probes, runs
from evo_agents.hub.server.runs.models import OBJECT_NAME

PROBE_RUN = "curator.probe_run"  # the night shift queued a probe run: "<project>/<plan> run:<id> probe:<id> week:<day>"
UNMEASURED = "the week's probes spent probes.budget_usd ({spent} of {cap}) before this probe ran on this model"
REQUEUED = ("stale", "failed", "unmeasured")  # the states of a week's row an admin puts back to queued

Reason = Annotated[str, Field(min_length=1, max_length=probes.MAX_REASON_CHARS)]


class ProbeSession(BaseModel):
    """One session of a probe run: its arm, which of the arm's k it is, whether the probe's verify passed on its work,
    and what the agent took, on the runtime and the model it actually ran."""

    model_config = ConfigDict(extra="forbid")

    arm: Literal[probes.ARMS] = Field(description="on: the change's rule in place; off: the change's diff taken off")  # pyright: ignore[reportInvalidTypeForm]
    index: int = Field(ge=1, le=probes.MAX_K, description="which of the arm's k sessions, from 1")
    passed: bool = Field(description="the probe's verify exited 0 on the session's work")
    exit_code: int | None = Field(None, description="the verify's; null when it did not run")
    cost_usd: float | None = Field(None, ge=0, description="the agent's session; null when its runtime names none")
    turns: int | None = Field(None, ge=0)
    duration_ms: int = Field(ge=0, description="the agent's time")
    runtime: Literal[runs.RUNTIMES]  # pyright: ignore[reportInvalidTypeForm]
    model: str | None = Field(None, max_length=200, description="the model the session ran on, as its runtime said it")
    agent_error: str | None = Field(None, max_length=probes.MAX_REASON_CHARS, description="why its turn ended short")
    session_id: str | None = Field(None, max_length=200)


class ProbeReport(BaseModel):
    """A session's result with the commits the arms run from and where (``arms_at``); or why the arms cannot be made,
    neither at the tip of the default branch nor at the change's commits (``stale``)."""

    model_config = ConfigDict(extra="forbid")

    base_sha: str | None = Field(None, pattern=OBJECT_NAME, description="the task's base commit the sessions start at")
    harness_sha: str | None = Field(
        None, pattern=OBJECT_NAME, description="the commit of the change's repo arm on runs with"
    )
    arms_at: Literal[probes.ARMS_AT] | None = Field(  # pyright: ignore[reportInvalidTypeForm]
        None,
        description="tip: arm on at the tip of the default branch, arm off that tip less the change's diff; change: "
        "the diff no longer comes off the tip, so arm on at the change's commit and arm off at the one before it. "
        "None from a worker that names none, which ran at the tip",
    )
    session: ProbeSession | None = None
    stale: Reason | None = Field(None, description="why the arms cannot be made: the probe is stale for the week")

    @model_validator(mode="after")
    def _one(self):
        if (self.session is None) == (self.stale is None):
            raise ValueError("a probe report is one session with its base_sha and harness_sha, or stale with why")
        if self.session is not None and (self.base_sha is None or self.harness_sha is None):
            raise ValueError("a probe session names the base_sha and harness_sha its arms ran from")
        return self


class ProbeRun(BaseModel):
    id: int
    probe_id: int
    change_id: int | None = Field(description="null: the probe of a plugin's rule, which has no change")
    week: date = Field(description="the Monday of the ISO week, in the charter's time zone")
    runtime: str
    model: str | None
    k: int = Field(description="the sessions of each arm")
    run_id: int | None
    state: Literal[probes.RUN_STATES]  # pyright: ignore[reportInvalidTypeForm]
    attempts: int
    base_sha: str | None
    harness_sha: str | None
    arms_at: Literal[probes.ARMS_AT] | None = Field(  # pyright: ignore[reportInvalidTypeForm]
        None, description="where the arms ran: tip or change; null without a session, and for a plugin's probe"
    )
    sessions: list[dict] = Field(description="ProbeSession, in the order they were reported")
    reason: str | None
    created_at: datetime
    updated_at: datetime


class ProbeRequeue(BaseModel):
    probe_id: int
    week: date = Field(description="the Monday of the ISO week in progress, in the charter's time zone")
    requeued: list[ProbeRun] = Field(
        description="the rows put back to queued, with no attempt: the next night runs them"
    )
    kept: list[ProbeRun] = Field(
        description="the week's other rows of the probe, queued, running or done, as they were"
    )
