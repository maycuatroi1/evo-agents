"""The shapes of the routes of a project's daily release (``routes.releases``), the audit actions of their writes and
the page sizes of the ended cycles."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from evo_agents.hub import release_ci, release_runs, releases
from evo_agents.hub.server.base.models import StrictModel

SET = "release.set"  # an admin set the schedule: "<project> repo=<repo> at=<HH:MM> <time zone>"
PAUSE = "release.pause"  # "<project>", the queued runs it cancelled and the cycle it closed in the log line
RESUME = "release.resume"
RUN = "release.run"  # an admin checked now: "<project> check:<id>"
ENDED = tuple(state for state in releases.CYCLE_STATES if state not in releases.UNFINISHED)  # published, closed
SHOWN_CYCLES = 20  # the ended cycles one page of GET .../releases/cycles lists by default
MAX_CYCLES = 100
MAX_CYCLES_OFFSET = 10_000


class ReleaseScheduleWrite(StrictModel):
    """What PUT .../releases/schedule sets; a key left out keeps what the schedule holds, or its default."""

    repo: str | None = Field(None, min_length=1, max_length=200, description="a repo of the project, on GitHub")
    at: str | None = Field(None, pattern=releases.AT.pattern, description="the local time of the daily check, HH:MM")
    timezone: str | None = Field(None, max_length=64, pattern=releases.TIME_ZONE.pattern, description="an IANA name")
    target_ref: str | None = Field(None, max_length=releases.MAX_REF_CHARS, description="the ref a release ships")
    tag_pattern: str | None = Field(None, pattern=releases.TAG_PATTERN.pattern, description="a prefix, then *")
    workflow: str | None = Field(None, pattern=releases.WORKFLOW.pattern, description="the workflow that publishes")
    paths: list[str] | None = Field(None, min_length=1, max_length=releases.MAX_PATHS, description="packaging paths")

    @field_validator("target_ref")
    @classmethod
    def _ref(cls, value: str | None) -> str | None:
        problem = None if value is None else releases.ref_problem(value)
        if problem:
            raise ValueError(problem)
        return value

    @field_validator("paths")
    @classmethod
    def _paths(cls, value: list[str] | None) -> list[str] | None:
        problem = None if value is None else releases.paths_problem(value)
        if problem:
            raise ValueError(problem)
        return value


class ReleaseSchedule(BaseModel):
    repo: str
    github_repo: str | None = Field(description="owner/name of the repo on GitHub; null once its origin is not there")
    at: str = Field(description="the local time of the daily check, HH:MM")
    timezone: str
    target_ref: str = Field(description="the ref whose commit a release ships: a tag, else a branch of that name")
    tag_pattern: str = Field(description="the release tags: a literal prefix, then *, before a version X.Y.Z")
    workflow: str = Field(description="the workflow that publishes a tag")
    paths: list[str] = Field(description="the packaging paths: a check opens a cycle when one of them changed")
    owner: str = Field(description="the admin who set the schedule last, whom its runs are dispatched as")
    updated_by: str
    created_at: datetime
    updated_at: datetime
    paused_at: datetime | None
    paused_by: str | None


class ReleaseCheck(BaseModel):
    id: int
    trigger: Literal[releases.CHECK_TRIGGERS] = Field(description="schedule, the daily job; manual, an admin's run")  # pyright: ignore[reportInvalidTypeForm]
    local_day: date = Field(description="the day of the check in the schedule's time zone")
    checked_at: datetime
    requested_by: str | None = Field(description="the admin of a manual check")
    outcome: Literal[releases.CHECK_OUTCOMES] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="opened: a cycle opened and its cut run queued; unchanged: no packaging path changed since the "
        "newest tag; busy: a cycle is open or stopped; paused: the schedule is paused; owner: its owner holds no "
        "admin role on the project; error: the check could not read GitHub, the reason says why"
    )
    reason: str | None
    base_tag: str | None = Field(description="the newest tag the pattern matched")
    base_commit: str | None
    target_commit: str | None = Field(description="the commit of the target ref")
    changed_paths: list[str] | None = Field(description="the packaging paths that changed since the newest tag")
    cycle_id: int | None
    run_id: int | None


class ReleaseCycleRun(BaseModel):
    run_id: int
    mode: Literal[releases.RUN_MODES]  # pyright: ignore[reportInvalidTypeForm]
    tag: str | None = Field(description="the red tag a fix run fixes forward")
    state: str
    cost_usd: float
    version: str | None = Field(None, description="the version the run pushed; null before it pushed")
    branch: str | None = Field(None, description="release/<version>, the branch it pushed")
    decision: str | None = Field(None, description="the agent's Decision line on the version")
    pull_url: str | None = Field(None, description="the pull request the hub opened of the branch")
    merge_commit: str | None = Field(None, description="the commit the hub merged it as")
    tagged: str | None = Field(None, description="the tag the hub made on the merge commit")
    tagged_at: datetime | None = None
    land_state: Literal[release_runs.LAND_STATES] | None = Field(  # pyright: ignore[reportInvalidTypeForm]
        None, description="tagged; refused, with the reason; failed, GitHub failing; null while it waits to land"
    )
    land_reason: str | None = None


class ReleaseTag(BaseModel):
    tag: str
    commit: str
    run_id: int | None = Field(description="the run whose merge commit the hub tagged; null for a tag a person made")
    ci_state: Literal[release_ci.CI_STATES] = Field(description="the CI of the schedule's workflow for the tag")  # pyright: ignore[reportInvalidTypeForm]
    workflow_url: str | None = Field(description="the run of the workflow for the tag")
    run_attempt: int | None
    reruns: int = Field(description="times the hub ran its failed jobs again, for a fault of the infrastructure")
    failed_tests: list[str] = Field(description="the test ids that failed, else each failed job and its step")
    failed_jobs: list[str]
    infra: bool = Field(description="whether the infrastructure alone failed it")
    problem: str | None = Field(description="what kept the hub from reading its CI: actions_permission, GitHub failing")
    watched_since: datetime = Field(description="what its deadlines count from")
    checked_at: datetime | None


class ReleaseCycle(BaseModel):
    id: int
    state: Literal[releases.CYCLE_STATES]  # pyright: ignore[reportInvalidTypeForm]
    start: Literal[releases.RUN_MODES] = Field(description="the mode of its first run")  # pyright: ignore[reportInvalidTypeForm]
    owner: str
    local_day: date
    base_tag: str | None
    base_commit: str | None
    target_ref: str
    target_commit: str | None
    changed_paths: list[str]
    cost_usd: float = Field(description="what they cost so far, each agent session once")
    stop_reason: str | None
    stop_cause: Literal[release_ci.STOP_CAUSES] | None = Field(description="why it stopped last")  # pyright: ignore[reportInvalidTypeForm]
    decision_id: int | None = Field(description="the decision its last stop asked the owner")
    thread: str | None = Field(description="the thread its last stop opened in the project's triage chat")
    opened_at: datetime
    ended_at: datetime | None
    runs: list[ReleaseCycleRun]
    tags: list[ReleaseTag] = Field(description="the tags it follows, in the order it began to")


class ReleaseCycleList(BaseModel):
    project: str
    cycles: list[ReleaseCycle] = Field(description="the cycles that ended, published or closed, newest first")
    total: int = Field(description="the cycles that ended, on every page")
    limit: int
    offset: int


class ReleaseStatus(BaseModel):
    project: str
    schedule: ReleaseSchedule | None = Field(description="null before an admin sets one")
    paused: bool
    next_check_at: datetime | None = Field(description="the next moment of the daily check, in UTC")
    last_check: ReleaseCheck | None
    checks: list[ReleaseCheck] = Field(description=f"the latest {releases.SHOWN_CHECKS} checks, newest first")
    cycle: ReleaseCycle | None = Field(description="the cycle that is open or stopped, if any")
