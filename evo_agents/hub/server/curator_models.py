"""The models of the night shift's routes (``evo_agents.hub.server.curator``): the charter an admin writes and shows,
its revisions, the schedules, the night, and where a project's Curator stands. ``evo_agents.hub.curator`` holds the
rules they check."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from evo_agents.hub import curator, ledger, runs
from evo_agents.hub.credentials import SECRET_NAME
from evo_agents.hub.judge import PLANNED_TIERS, PlannableTier
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.charter_settings import (
    HistorySettings,
    ProbeModel,
    ProbeSettings,
    ReplaySettings,
    ReviewSettings,
)
from evo_agents.hub.server.runs.models import ModelName

CuratorState = Literal["running", "on_duty", "idle", "paused"]
CURATOR_STATES: tuple[str, ...] = get_args(CuratorState)  # where a project's night shift stands, for people


TimeOfDay = Annotated[str, Field(pattern=curator.TIME_OF_DAY, description="HH:MM, 24 hours, in the window's zone")]
PathGlob = Annotated[str, Field(min_length=1, max_length=curator.MAX_PATH_CHARS, pattern=r"^[^\x00-\x1f\x7f]+$")]
Check = Annotated[str, Field(min_length=1, max_length=curator.MAX_CHECK_CHARS, pattern=r"^[^\x00]+$")]
PlanIdentifier = Annotated[str, Field(pattern=plan_routes.PLAN_ID)]
SecretName = Annotated[str, Field(pattern=f"^{SECRET_NAME.pattern}$", description="a secret of the schedule's owner")]


class Window(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: TimeOfDay
    end: TimeOfDay = Field(description="when it is not after start, the window runs past midnight")
    timezone: str = Field(
        min_length=1,
        max_length=curator.MAX_TIME_ZONE_CHARS,
        pattern=curator.TIME_ZONE,
        description="an IANA time zone, such as Asia/Ho_Chi_Minh",
    )

    @model_validator(mode="after")
    def _not_empty(self):
        if self.start == self.end:
            raise ValueError("the window's start and end are the same time: give it a length")
        return self


class PacingQuietHours(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: TimeOfDay
    end: TimeOfDay = Field(description="when it is not after start, the quiet hours run past midnight")

    @model_validator(mode="after")
    def _not_empty(self):
        if self.start == self.end:
            raise ValueError("the quiet hours' start and end are the same time: give them a length")
        return self


class Pacing(BaseModel):
    """The Curator runs through the day instead of in a window: at any hour outside ``quiet_hours``, spending the
    period's budget evenly (``curator.pace_line``)."""

    model_config = ConfigDict(extra="forbid")

    period: curator.PacingPeriod = Field(description="day, from local midnight, or week, from Monday 00:00 local")
    budget_usd: float = Field(gt=0, le=curator.MAX_PERIOD_BUDGET_USD, description="the most a period may cost")
    quiet_hours: PacingQuietHours | None = Field(
        None, description="local hours no run of the Curator is queued; null: none"
    )
    timezone: str = Field(
        min_length=1,
        max_length=curator.MAX_TIME_ZONE_CHARS,
        pattern=curator.TIME_ZONE,
        description="an IANA time zone, such as Asia/Ho_Chi_Minh",
    )


class Goal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=curator.GOAL_ID)
    what: str = Field(min_length=1, max_length=curator.MAX_GOAL_CHARS)


class Role(BaseModel):
    """The runtime and model one role of the Curator runs on."""

    model_config = ConfigDict(extra="forbid")

    runtime: runs.Runtime = curator.DEFAULT_RUNTIME
    model: ModelName | None = Field(None, description="as the runtime names it; null: the runtime's own choice")


class Judge(Role):
    hidden_checks: list[Check] | None = Field(
        default_factory=list,
        max_length=curator.MAX_HIDDEN_CHECKS,
        description="commands the Judge runs that no Builder sees; shown to the project's admins alone, null to "
        "anyone else, and null in a write keeps those of the newest revision",
    )


BackoffMinutes = Annotated[int, Field(ge=curator.INFRA_BACKOFF_RANGE[0], le=curator.INFRA_BACKOFF_RANGE[1])]


class InfraBreaker(BaseModel):
    """How the night shift holds off the kinds of run whose dependency (the hub, or a runtime and its account) failed
    (``curator.outages``); a charter written before it gets the defaults."""

    model_config = ConfigDict(extra="forbid")

    backoff_minutes: list[BackoffMinutes] = Field(
        default_factory=lambda: list(curator.INFRA_BACKOFF_MINUTES),
        min_length=curator.INFRA_BACKOFFS[0],
        max_length=curator.INFRA_BACKOFFS[1],
        description="the wait before the probe after the 1st, 2nd... failure in a row of a dependency, the last one "
        "past their end; each is jittered by 20% either way",
    )
    max_probes_per_day: int = Field(
        curator.INFRA_PROBES_PER_DAY,
        ge=curator.INFRA_PROBES[0],
        le=curator.INFRA_PROBES[1],
        description="probes of a failing dependency in a day; then its kinds of run wait and the owner is told once",
    )


class CircuitBreaker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_failed_in_a_row: int = Field(
        2,
        ge=curator.CIRCUIT_BREAKER[0],
        le=curator.CIRCUIT_BREAKER[1],
        description="jobs of a night in a row that failed of the agent's cause or were reverted before the hub pauses "
        "the night shift; a run that failed of an infra cause (curator.INFRA_CAUSES) does not count",
    )
    infra: InfraBreaker = Field(default_factory=lambda: InfraBreaker.model_validate({}))


class CharterBody(BaseModel):
    """What an admin writes."""

    model_config = ConfigDict(extra="forbid")

    goals: list[Goal] = Field(
        default_factory=list, max_length=curator.MAX_GOALS, description="the product's goals, highest first"
    )
    window: Window | None = Field(None, description="the night the Curator runs in; null with pacing")
    pacing: Pacing | None = Field(
        None, description="the Curator runs through the day on the period's budget instead of a window; null: a window"
    )
    night_budget_usd: float | None = Field(
        None,
        gt=0,
        le=curator.MAX_NIGHT_BUDGET_USD,
        description="the most a night may cost, with a window; null with pacing, whose budget_usd it is",
    )
    run_budget_usd: float | None = Field(
        None, gt=0, le=curator.MAX_NIGHT_BUDGET_USD, description="the most one run may cost; null: what the night has"
    )
    run_max_turns: int = Field(300, ge=1, le=curator.MAX_TURNS, description="turns of the agent in one run")
    run_minutes: int = Field(
        120, ge=curator.RUN_MINUTES[0], le=curator.RUN_MINUTES[1], description="agent time one run may use"
    )
    max_runs_per_night: int = Field(6, ge=1, le=curator.MAX_RUNS_PER_NIGHT)
    night_plans: list[PlanIdentifier] = Field(
        default_factory=list,
        max_length=curator.MAX_NIGHT_PLANS,
        description="the plans the night shift may run, in the order it takes them",
    )
    max_decisions_per_day: int = Field(5, ge=0, le=curator.MAX_DECISIONS_PER_DAY)
    brief_at: TimeOfDay = "07:00"
    planned_tiers: list[PlannableTier] = Field(
        default_factory=lambda: list(PLANNED_TIERS),
        description="the tiers whose accepted proposals become the Curator's plans, which the night shift builds; "
        "tier 3 only when named",
    )
    auto_merge: list[curator.AutoMergeTier] = Field(
        default_factory=list, description="the tiers the hub may merge by itself"
    )
    merge_without_ci: bool = Field(
        False,
        description="the hub may merge a change the Judge passed when no CI ran on it or the ruleset requires no "
        "check; a check that ran must still pass",
    )
    answer_by: curator.Answerer = Field(
        "owner",
        description="who answers the proposals: owner, the project's admins; agent, the project's default agent in a "
        "thread of the triage chat, for the schedule's owner",
    )
    protected_paths: list[PathGlob] = Field(
        default_factory=list,
        max_length=curator.MAX_PROTECTED_PATHS,
        description="globs no change of the Curator may touch below tier 3",
    )
    circuit_breaker: CircuitBreaker = Field(default_factory=lambda: CircuitBreaker.model_validate({}))
    outcome_days: int = Field(
        ledger.DEFAULT_OUTCOME_DAYS,
        ge=ledger.OUTCOME_DAYS[0],
        le=ledger.OUTCOME_DAYS[1],
        description="the days after a merge over which the hub counts a change's figures again, and then keeps it "
        "or proposes its revert",
    )
    review: ReviewSettings = Field(default_factory=lambda: ReviewSettings.model_validate({}))
    replay: ReplaySettings = Field(default_factory=lambda: ReplaySettings.model_validate({}))
    history: HistorySettings = Field(default_factory=lambda: HistorySettings.model_validate({}))
    probes: ProbeSettings = Field(default_factory=lambda: ProbeSettings.model_validate({}))
    reviewer: Role = Field(default_factory=lambda: Role.model_validate({}))
    builder: Role = Field(default_factory=lambda: Role.model_validate({}))
    judge: Judge = Field(default_factory=lambda: Judge.model_validate({}))
    git_secret: SecretName | None = Field(
        None,
        description="the owner's git secret the Curator's runs use for origins not on GitHub (GitLab, where it should "
        "hold the Developer role); null: none, so no Builder runs there",
    )
    env_secrets: list[SecretName] = Field(
        default_factory=list,
        max_length=curator.MAX_ENV_SECRETS,
        description="the owner's env secrets the Curator's runs get, by name; they get no other",
    )

    @field_validator("goals")
    @classmethod
    def _goal_ids_once(cls, goals: list[Goal]) -> list[Goal]:
        ids = [goal.id for goal in goals]
        repeated = sorted({goal for goal in ids if ids.count(goal) > 1})
        if repeated:
            raise ValueError(f"goal ids are named once each: {', '.join(repeated)}")
        return goals

    @field_validator("night_plans", "protected_paths", "env_secrets")
    @classmethod
    def _once(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @field_validator("auto_merge", "planned_tiers")
    @classmethod
    def _tiers(cls, tiers: list[int]) -> list[int]:
        return sorted(set(tiers))

    @model_validator(mode="after")
    def _run_within_night(self):
        if self.pacing is not None and self.window is not None:
            raise ValueError(
                "a charter with pacing has no window: drop window, as the Curator runs at any hour outside "
                "pacing.quiet_hours"
            )
        if self.pacing is not None and self.night_budget_usd is not None:
            raise ValueError("a charter with pacing has no night_budget_usd: its budget is pacing.budget_usd")
        if self.pacing is None and self.window is None:
            raise ValueError("a charter needs a window, or pacing to run through the day")
        if self.pacing is None and self.night_budget_usd is None:
            raise ValueError("a charter with a window needs night_budget_usd")
        most, name_of = (
            (self.pacing.budget_usd, "pacing.budget_usd")
            if self.pacing
            else (self.night_budget_usd, "night_budget_usd")
        )
        if self.run_budget_usd is not None and self.run_budget_usd > most:
            raise ValueError(f"run_budget_usd is over {name_of}: a run never gets more than the night has")
        caps = (("review", self.review.budget_usd), ("replay", self.replay.budget_usd))
        for name, cap in (*caps, ("history", self.history.budget_usd)):
            if cap is not None and cap > most:
                raise ValueError(f"{name}.budget_usd is over {name_of}: a run never gets more than the night has")
        if not self.probes.models:  # the probes run on the builder's runtime and model unless the charter names others
            self.probes.models = [ProbeModel(runtime=self.builder.runtime, model=self.builder.model)]
        return self


class CharterWrite(CharterBody):
    @model_validator(mode="before")
    @classmethod
    def _shown_keys(cls, data):
        if isinstance(data, dict):
            return {key: value for key, value in data.items() if key not in curator.CHARTER_META}
        return data


class Charter(CharterBody):
    project: str
    revision: int
    updated_by: str = Field(description="who wrote this revision")
    updated_at: datetime
    schedule_owner: str = Field(description="the member the night shift dispatches its runs as: the pool's owner")


class CharterRevision(BaseModel):
    revision: int
    updated_by: str
    updated_at: datetime


class Schedule(BaseModel):
    id: int
    kind: curator.ScheduleKind
    owner: str = Field(description="the member its runs are dispatched as")
    paused_at: datetime | None
    paused_by: str | None = Field(description="the member who paused it; null when the hub did, or while it runs")
    pause_reason: str | None = Field(
        None, description="why the hub paused it, its circuit breaker; null when a member did, or while it runs"
    )


class Night(BaseModel):
    night: date = Field(
        description="the local date the window opened on: the night now, or the last one; with pacing, the period's "
        "first date"
    )
    in_window: bool = Field(description="inside the window; with pacing, outside the quiet hours")
    local_time: str = Field(description="HH:MM now, in the charter's time zone")
    runs: int = Field(description="the runs the night shift queued that night")
    max_runs: int
    cost_usd: float = Field(description="what they cost, from their usage, each agent session once")
    budget_usd: float
    active_run_id: int | None = Field(description="the run of the schedule queued or held now, if any")


class ReviewRunSummary(BaseModel):
    """The last review run of a project: the run, the night it reviewed, and what it wrote."""

    id: int
    state: runs.RunState
    night: date
    lenses: list[str] = Field(description="the lenses of its night")
    findings: int
    proposals: int
    queued_at: datetime
    finished_at: datetime | None


class BriefSummary(BaseModel):
    """The project's last morning brief (``evo_agents.hub.server.brief``)."""

    id: int
    day: date = Field(description="the local day it was sent on, in the charter's time zone")
    night: date = Field(description="the night it reports on")
    sent_at: datetime
    to: str = Field(description="the member it went to: the owner of the night shift's schedule")
    notification_id: int | None = Field(description="the notification of kind notice (curator_brief) that carried it")
    title: str


class CuratorStatus(BaseModel):
    project: str
    charter: Charter | None
    paused: bool = Field(description="every schedule of the project is paused")
    schedules: list[Schedule]
    night: Night | None = Field(description="null without a charter")
    last_review_run: ReviewRunSummary | None = Field(
        None, description="the project's latest review run, of any night; null before the first"
    )
    state: CuratorState | None = Field(
        None,
        description="paused; running while a run of the night shift is queued or held; on_duty in the window with "
        "none; idle outside it; null without a charter",
    )
    open_proposals: int = Field(0, description="the project's proposals that wait for an answer, that you may read")
    last_brief: BriefSummary | None = Field(None, description="the project's last morning brief; null before the first")


class CuratorOverview(BaseModel):
    """Where a project's Curator stands, for Home: GET /v1/me/overview names it for each project with a charter."""

    state: CuratorState = Field(description="as GET /v1/projects/{p}/curator says it")
    in_window: bool = Field(description="the charter's window is open now; with pacing, outside its quiet hours")
    active_run_id: int | None = Field(description="the run of the night shift queued or held now, if any")
    open_proposals: int = Field(description="the project's proposals that wait for an answer, that you may read")


class NightReview(BaseModel):
    id: int
    state: runs.RunState
    findings: int
    proposals: int


class NightSummary(BaseModel):
    """One night of a project's night shift: what it queued, how those runs ended, what they cost, and its review."""

    night: date = Field(description="the local date its window opened on")
    runs: int = Field(description="the runs the night shift queued that night, its review run included")
    done: int
    failed: int = Field(description="failed or lost")
    cancelled: int
    active: int = Field(description="not ended yet")
    cost_usd: float = Field(description="what they cost, from their usage, each agent session once")
    review_run: NightReview | None = Field(description="the night's review run, if one was queued")
    figures: bool = Field(description="curator.collect counted the night's figures")


class NightList(BaseModel):
    project: str
    nights: list[NightSummary] = Field(description="the latest night first")
    budget_usd: float | None = Field(description="the night's budget of the newest charter; null without one")
    max_runs: int | None = Field(description="the night's runs of the newest charter; null without one")
