"""The night shift on the hub: a project's charter, its schedules, pausing them, and the job that queues their runs.
``evo_agents.hub.curator`` holds the model: the window, the night, the caps and how a run's cost is read.

GET /v1/projects/{p}/curator/charter (reader) shows the project's charter at its newest revision, or at ``?revision``;
404 when it has none. The checks of the Judge (``judge.hidden_checks``) are shown to the project's admins alone, and as
null to anyone else. GET .../curator/charter/revisions lists every revision, newest first: none is ever deleted.

PUT .../curator/charter (admin of the project) writes a new revision: the window's time zone must be one Postgres
knows; the worker on duty, named by ``worker``, or the pool, named by ``pool`` (exactly one of the two), stays when it
is the one of the newest revision, and is otherwise one of the caller's own live workers that serves the project and
takes runs dispatched by the hub (its dispatch_from is not web), or one of the caller's own pools that lists the
project, else 403 or 409 (``evo_agents.hub.server.night_duty``). The project's schedule (one per kind, here
``night_shift``) follows it: its owner, the member it dispatches as, is that worker's or pool's owner. A body equal to
the newest revision's writes nothing and answers that revision. ``judge.hidden_checks`` null keeps the checks of the
newest revision. The keys ``charter show --json`` prints besides the body (``curator.CHARTER_META``) are ignored, so
what was shown can be written back. Each revision is audited (curator.charter). A member without the admin role gets
403, a worker token 403 from the guard of /v1, before any of this.

GET /v1/projects/{p}/curator (reader) says where the night shift stands: the charter, whether it is paused, each
schedule with its owner and worker, and the night now (or the last one, outside the window): its runs and its cost,
the run of the schedule that is queued or held, if any. POST .../curator/pause (an admin of the project, or the owner of
one of its schedules) pauses every schedule of the project at once and cancels each run they queued that is still
queued and resumes no parked run; .../curator/resume lets them run again, a night shift the circuit breaker paused
included, and starts its count of jobs gone wrong again. Each is audited (curator.pause,
curator.resume); a second pause keeps the first one's time. GET .../curator says the night shift's ``state`` in a word
(CURATOR_STATES) and how many of the project's proposals wait for an answer; ``curator_overview`` says the same of
every project of the caller for GET /v1/me/overview. GET .../curator/nights (reader) lists the latest nights, newest
first: for each, the runs the schedules queued for it by how they stand, their cost as a night's cost is counted, its
review run with what it wrote, and whether curator.collect counted its figures.

``fire_schedules`` is the job ``hub.fire_schedules``, run every minute by the hub's worker: for each schedule of kind
night_shift, under its row's lock (a schedule another run of the job holds is passed over), it first lets the circuit
breaker look at the night (``outcomes.check_circuit``: once the charter's ``circuit_breaker.max_failed_in_a_row`` jobs
of the night in a row failed or were reverted, every schedule of the project is paused with the reason in
``pause_reason`` and no member in ``paused_by``, and their owner gets the notice curator_paused), then it cancels its
queued runs when it is paused, and outside its window those of the night (``shift_hours``), and otherwise queues at most
one run (``gate_of`` says when it may): the night's review run first, when the night has none yet and the worker runs
review runs (``evo_agents.hub.server.collect``), else the night's history run once its review run was queued
(``evo_agents.hub.server.history``), these two inside the window alone, else the judge run of a change of the Curator
that waits for one, else the Builder of a change the Curator planned (``evo_agents.hub.server.changes``), else the
replay run of a change that waits for one (``evo_agents.hub.server.replays``), else, inside the window, a plan run of
the charter's night_plans, as ``evo_agents.hub.curator`` says, audited as curator.dispatch with the owner as actor and
no token, else, when no plan of night_plans has work, the probe run of a probe and a model the week has not measured,
within the charter's probes.budget_usd (``evo_agents.hub.server.probe_runs``). GET .../curator also names the last review run of the project, with how many findings and proposals it wrote,
and its last morning brief (``evo_agents.hub.server.brief``). A night's cost is that of its runs (their ``usage``), each
agent session once at its largest total, as Claude Code reports a session's running total. The charter also names what
the Curator's runs get of the owner's secrets (``git_secret``, ``env_secrets``): no other secret is leased to them.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import (
    Text,
    and_,
    cast,
    column,
    exists,
    func,
    insert,
    literal,
    or_,
    select,
    table,
    union,
    update,
)
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import curator, ledger, runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.credentials import SECRET_NAME
from evo_agents.hub.db import one_of
from evo_agents.hub.judge import PLANNABLE_TIERS, PLANNED_TIERS
from evo_agents.hub.server import audit, hub_settings
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.charter_settings import (
    HistorySettings,
    ProbeModel,
    ProbeSettings,
    ReplaySettings,
    ReviewSettings,
)
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.night_cost import QUEUED_OR_HELD, NightFigures, next_run_budget, night_figures, usage_cost
from evo_agents.hub.server.night_duty import Duty, DutyTarget, duty_target, on_duty
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.run_state import notify_queued, write_event
from evo_agents.hub.server.runs.models import ModelName
from evo_agents.hub.server.runs.models.placement import PoolName
from evo_agents.hub.server.runs.service.dispatch import insert_run, plan_run_repos
from evo_agents.hub.server.runs.service.views import lock_plan, plan_activity
from evo_agents.hub.server.security import MACHINE, CurrentUser, Principal
from evo_agents.hub.server.shift_hours import cancel_queued, changes_waiting
from evo_agents.hub.server.worker_models import WORKER_NAME

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})

CHARTER = "curator.charter"  # an admin wrote a revision: "<project> revision=<n> worker=<name>" (or pool=<name>)
PAUSE = "curator.pause"  # every schedule of the project paused: "<project>"
RESUME = "curator.resume"  # and let run again: "<project>"
DISPATCH = "curator.dispatch"  # a schedule queued a plan run: "<project>/<plan> run:<id> schedule:<id> night=<date>"
NIGHT_SHIFT = "night_shift"
MAX_CHARTER_REVISIONS = 1000  # the revisions GET .../charter/revisions lists at most
CURATOR_STATES = ("running", "on_duty", "idle", "paused")  # where a project's night shift stands, for people
DEFAULT_NIGHTS, MAX_NIGHTS = 14, 90  # the nights GET .../curator/nights lists, by default and at most
PG_TIMEZONES = table("pg_timezone_names", column("name", Text))

TimeOfDay = Annotated[str, Field(pattern=curator.TIME_OF_DAY, description="HH:MM, 24 hours, in the window's zone")]
PathGlob = Annotated[str, Field(min_length=1, max_length=curator.MAX_PATH_CHARS, pattern=r"^[^\x00-\x1f\x7f]+$")]
Check = Annotated[str, Field(min_length=1, max_length=curator.MAX_CHECK_CHARS, pattern=r"^[^\x00]+$")]
PlanIdentifier = Annotated[str, Field(pattern=plan_routes.PLAN_ID)]
SecretName = Annotated[str, Field(pattern=f"^{SECRET_NAME.pattern}$", description="a secret of the schedule's owner")]


# Models


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


class Goal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=curator.GOAL_ID)
    what: str = Field(min_length=1, max_length=curator.MAX_GOAL_CHARS)


class Role(BaseModel):
    """The runtime and model one role of the Curator runs on."""

    model_config = ConfigDict(extra="forbid")

    runtime: Literal[runs.RUNTIMES] = curator.DEFAULT_RUNTIME
    model: ModelName | None = Field(None, description="as the runtime names it; null: the runtime's own choice")


class Judge(Role):
    hidden_checks: list[Check] | None = Field(
        default_factory=list,
        max_length=curator.MAX_HIDDEN_CHECKS,
        description="commands the Judge runs that no Builder sees; shown to the project's admins alone, null to "
        "anyone else, and null in a write keeps those of the newest revision",
    )


class CircuitBreaker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_failed_in_a_row: int = Field(
        2,
        ge=curator.CIRCUIT_BREAKER[0],
        le=curator.CIRCUIT_BREAKER[1],
        description="jobs of a night in a row that failed or were reverted before the hub pauses the night shift",
    )


class CharterBody(BaseModel):
    """What an admin writes."""

    model_config = ConfigDict(extra="forbid")

    goals: list[Goal] = Field(
        default_factory=list, max_length=curator.MAX_GOALS, description="the product's goals, highest first"
    )
    window: Window
    worker: str | None = Field(
        None, pattern=WORKER_NAME, description="the worker on duty, by name: one of the writer's own; or name a pool"
    )
    pool: PoolName | None = Field(
        None,
        description="the pool on duty, by name: one of the writer's own; its ready nodes take the night's runs, any "
        "of them, by the selector pool=<name>; or name a worker",
    )
    night_budget_usd: float = Field(gt=0, le=curator.MAX_NIGHT_BUDGET_USD, description="the most a night may cost")
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
    planned_tiers: list[Literal[PLANNABLE_TIERS]] = Field(
        default_factory=lambda: list(PLANNED_TIERS),
        description="the tiers whose accepted proposals become the Curator's plans, which the night shift builds; "
        "tier 3 only when named",
    )
    auto_merge: list[Literal[curator.AUTO_MERGE_TIERS]] = Field(
        default_factory=list, description="the tiers the hub may merge by itself"
    )
    merge_without_ci: bool = Field(
        False,
        description="the hub may merge a change the Judge passed when no CI ran on it or the ruleset requires no "
        "check; a check that ran must still pass",
    )
    protected_paths: list[PathGlob] = Field(
        default_factory=list,
        max_length=curator.MAX_PROTECTED_PATHS,
        description="globs no change of the Curator may touch below tier 3",
    )
    circuit_breaker: CircuitBreaker = Field(default_factory=CircuitBreaker)
    outcome_days: int = Field(
        ledger.DEFAULT_OUTCOME_DAYS,
        ge=ledger.OUTCOME_DAYS[0],
        le=ledger.OUTCOME_DAYS[1],
        description="the days after a merge over which the hub counts a change's figures again, and then keeps it "
        "or proposes its revert",
    )
    review: ReviewSettings = Field(default_factory=ReviewSettings)
    replay: ReplaySettings = Field(default_factory=ReplaySettings)
    history: HistorySettings = Field(default_factory=HistorySettings)
    probes: ProbeSettings = Field(default_factory=ProbeSettings)
    reviewer: Role = Field(default_factory=Role)
    builder: Role = Field(default_factory=Role)
    judge: Judge = Field(default_factory=Judge)
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
        if (self.worker is None) == (self.pool is None):
            raise ValueError("a charter names the worker on duty (worker) or the pool (pool), exactly one of the two")
        if self.run_budget_usd is not None and self.run_budget_usd > self.night_budget_usd:
            raise ValueError("run_budget_usd is over night_budget_usd: a run never gets more than the night has")
        caps = (("review", self.review.budget_usd), ("replay", self.replay.budget_usd))
        for name, cap in (*caps, ("history", self.history.budget_usd)):
            if cap is not None and cap > self.night_budget_usd:
                raise ValueError(
                    f"{name}.budget_usd is over night_budget_usd: a run never gets more than the night has"
                )
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
    worker_id: int | None = Field(description="the worker on duty; null for a pool")
    pool_id: int | None = Field(None, description="the pool on duty; null for a worker")
    schedule_owner: str = Field(
        description="the member the night shift dispatches its runs as: the worker's, or the pool's, owner"
    )


class CharterRevision(BaseModel):
    revision: int
    updated_by: str
    updated_at: datetime
    worker: str | None = Field(description="the worker on duty; null for a pool")
    worker_id: int | None
    pool: str | None = Field(None, description="the pool on duty; null for a worker")
    pool_id: int | None = None


class Schedule(BaseModel):
    id: int
    kind: Literal[curator.SCHEDULE_KINDS]
    owner: str = Field(description="the member its runs are dispatched as")
    worker_id: int | None
    worker: str | None = Field(description="the worker its runs go to; null for a pool")
    pool_id: int | None = None
    pool: str | None = Field(None, description="the pool whose ready nodes take its runs; null for a worker")
    paused_at: datetime | None
    paused_by: str | None = Field(description="the member who paused it; null when the hub did, or while it runs")
    pause_reason: str | None = Field(
        None, description="why the hub paused it, its circuit breaker; null when a member did, or while it runs"
    )


class Night(BaseModel):
    night: date = Field(description="the local date the window opened on: the night now, or the last one")
    in_window: bool
    local_time: str = Field(description="HH:MM now, in the charter's time zone")
    runs: int = Field(description="the runs the night shift queued that night")
    max_runs: int
    cost_usd: float = Field(description="what they cost, from their usage, each agent session once")
    budget_usd: float
    active_run_id: int | None = Field(description="the run of the schedule queued or held now, if any")


class ReviewRunSummary(BaseModel):
    """The last review run of a project: the run, the night it reviewed, and what it wrote."""

    id: int
    state: Literal[runs.RUN_STATES]
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


CuratorState = Literal[CURATOR_STATES]


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
    in_window: bool = Field(description="the charter's window is open now")
    active_run_id: int | None = Field(description="the run of the night shift queued or held now, if any")
    open_proposals: int = Field(description="the project's proposals that wait for an answer, that you may read")


class NightReview(BaseModel):
    id: int
    state: Literal[runs.RUN_STATES]
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


# Reading


def _admin(access: ProjectAccess, doing: str) -> None:
    if not has_role(access.role, "admin"):
        held = access.role or "no grant"
        raise HTTPException(403, f"{doing} of project {access.name} needs the admin role on it; you hold {held}")


def _reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise HTTPException(403, f"reading the night shift of project {access.name} needs a grant on it")


def _charters():
    """A charter revision with its writer's login, its worker's or pool's name and that one's owner's login."""
    c, w, writer, owner = tables.charters, tables.workers, tables.users.alias("writer"), tables.users.alias("owner")
    p = tables.worker_pools
    return (
        select(
            c.c.project_id,
            c.c.revision,
            c.c.body,
            c.c.worker_id,
            c.c.pool_id,
            c.c.created_at.label("updated_at"),
            writer.c.login.label("updated_by"),
            w.c.name.label("worker_name"),
            p.c.name.label("pool_name"),
            owner.c.login.label("schedule_owner"),
        )
        .join_from(c, writer, writer.c.id == c.c.created_by)
        .outerjoin(w, w.c.id == c.c.worker_id)
        .outerjoin(p, p.c.id == c.c.pool_id)
        .join(owner, owner.c.id == func.coalesce(w.c.owner_id, p.c.owner_id))
    )


async def _charter_row(conn: AsyncConnection, project_id: int, revision: int | None = None):
    c = tables.charters
    query = _charters().where(c.c.project_id == project_id)
    if revision is not None:
        query = query.where(c.c.revision == revision)
    return (await conn.execute(query.order_by(c.c.revision.desc()).limit(1))).one_or_none()


def _charter_view(project: str, row, *, admin: bool) -> Charter:
    body = dict(row.body)
    if not admin:
        body["judge"] = {**body.get("judge", {}), "hidden_checks": None}
    return Charter(
        **body,
        project=project,
        revision=row.revision,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
        worker_id=row.worker_id,
        pool_id=row.pool_id,
        schedule_owner=row.schedule_owner,
    )


def _no_charter(project: str) -> HTTPException:
    return HTTPException(
        404, f"project {project} has no charter: an admin writes one with `evo-agents hub curator charter set`"
    )


@router.get(
    "/{project}/curator/charter",
    response_model=Charter,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def show_charter(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    revision: Annotated[int | None, Query(ge=1, le=2**31 - 1, description="an older revision")] = None,
) -> Charter:
    """The project's charter, at its newest revision or at ``revision``."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        row = await _charter_row(conn, access.project_id, revision)
    if row is None:
        if revision is not None:
            raise HTTPException(404, f"project {project} has no charter revision {revision}")
        raise _no_charter(project)
    return _charter_view(access.name, row, admin=has_role(access.role, "admin"))


@router.get(
    "/{project}/curator/charter/revisions",
    response_model=list[CharterRevision],
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def charter_revisions(request: Request, project: ProjectName, user: CurrentUser) -> list[CharterRevision]:
    """Every revision of the project's charter, newest first; empty without one."""
    c = tables.charters
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        query = _charters().where(c.c.project_id == access.project_id).order_by(c.c.revision.desc())
        rows = (await conn.execute(query.limit(MAX_CHARTER_REVISIONS))).all()
    return [
        CharterRevision(
            revision=row.revision,
            updated_by=row.updated_by,
            updated_at=row.updated_at,
            worker=row.worker_name,
            worker_id=row.worker_id,
            pool=row.pool_name,
            pool_id=row.pool_id,
        )
        for row in rows
    ]


# Writing


async def _time_zone_known(conn: AsyncConnection, name: str) -> None:
    known = (await conn.execute(select(exists().where(PG_TIMEZONES.c.name == name)))).scalar_one()
    if not known:
        raise HTTPException(422, f"{name!r} is not a time zone the hub knows: give an IANA name such as Asia/Tokyo")


def _stored(body: CharterWrite, target: DutyTarget, current) -> dict:
    """The body to keep: the worker or pool under its own name, the other key left out, and the Judge's checks of
    ``current`` when the write gives none."""
    stored = body.model_dump(mode="json")
    stored.pop("pool" if target.key == "worker" else "worker")
    stored[target.key] = target.name
    if stored["judge"]["hidden_checks"] is None:
        kept = (current.body.get("judge") or {}).get("hidden_checks") if current is not None else None
        stored["judge"]["hidden_checks"] = kept or []
    return stored


async def _keep_schedule(conn: AsyncConnection, project_id: int, target: DutyTarget) -> None:
    s = tables.schedules
    kept = {"owner_id": target.owner_id, "worker_id": target.worker_id, "pool_id": target.pool_id}
    statement = pg_insert(s).values(project_id=project_id, kind=NIGHT_SHIFT, **kept)
    statement = statement.on_conflict_do_update(
        constraint="schedules_project_id_kind_key",
        set_={**kept, "updated_at": func.now()},
        where=or_(*(s.c[key].is_distinct_from(value) for key, value in kept.items())),
    )
    await conn.execute(statement)


@router.put(
    "/{project}/curator/charter",
    response_model=Charter,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
)
async def write_charter(request: Request, project: ProjectName, body: CharterWrite, user: CurrentUser) -> Charter:
    """Write a new revision of the project's charter; the admins of the project alone may."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _admin(access, "changing the charter")
        current = await _charter_row(conn, access.project_id)
        await _time_zone_known(conn, body.window.timezone)
        target = await duty_target(conn, user, access, body.worker, body.pool, current)
        stored = _stored(body, target, current)
        if len(json.dumps(stored, ensure_ascii=False).encode()) > curator.MAX_CHARTER_BYTES:
            raise HTTPException(413, f"the charter is over {curator.MAX_CHARTER_BYTES // 1024} KiB as JSON")
        same = current is not None and (current.worker_id, current.pool_id) == (target.worker_id, target.pool_id)
        if same and current.body == stored:
            return _charter_view(access.name, current, admin=True)
        revision = 1 if current is None else current.revision + 1
        values = {
            "project_id": access.project_id,
            "revision": revision,
            "body": stored,
            "worker_id": target.worker_id,
            "pool_id": target.pool_id,
            "created_by": user.user_id,
        }
        try:
            async with conn.begin_nested():
                await conn.execute(insert(tables.charters).values(**values))
        except IntegrityError:
            raise HTTPException(
                409, f"the charter of project {project} changed while this write was made: read it again"
            ) from None
        await _keep_schedule(conn, access.project_id, target)
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=CHARTER,
            target=f"{access.name} revision={revision} {target.where}",
            project_id=access.project_id,
        )
        row = await _charter_row(conn, access.project_id, revision)
    log.info("charter written", extra={"project": project, "revision": revision, "login": user.login})
    return _charter_view(access.name, row, admin=True)


# Where the night shift stands


def _schedules(project_id: int):
    s, w, owner, pauser = tables.schedules, tables.workers, tables.users.alias("owner"), tables.users.alias("pauser")
    p = tables.worker_pools
    return (
        select(
            s.c.id,
            s.c.kind,
            s.c.owner_id,
            owner.c.login.label("owner"),
            s.c.worker_id,
            w.c.name.label("worker"),
            s.c.pool_id,
            p.c.name.label("pool"),
            s.c.paused_at,
            pauser.c.login.label("paused_by"),
            s.c.pause_reason,
        )
        .join_from(s, owner, owner.c.id == s.c.owner_id)
        .outerjoin(w, w.c.id == s.c.worker_id)
        .outerjoin(p, p.c.id == s.c.pool_id)
        .outerjoin(pauser, pauser.c.id == s.c.paused_by)
        .where(s.c.project_id == project_id)
        .order_by(s.c.kind)
    )


def curator_state(paused: bool, active_run_id: int | None, in_window: bool) -> str:
    """One of CURATOR_STATES: paused first, then a run in flight, then the window."""
    if paused:
        return "paused"
    if active_run_id is not None:
        return "running"
    return "on_duty" if in_window else "idle"


def _newest_charters():
    """The newest charter of each project of the list bound as ``projects``, with the time now in its time zone."""
    c = tables.charters
    local_now = func.timezone(c.c.body["window"]["timezone"].astext, func.now())
    return (
        select(c.c.project_id, c.c.body, local_now.label("local_now"))
        .where(one_of(c.c.project_id, name="projects"))
        .ext(distinct_on(c.c.project_id))
        .order_by(c.c.project_id, c.c.revision.desc())
    )


def _schedules_held():
    """Each schedule of the projects bound as ``projects``: its project, when it was paused, and its first run queued
    or held now (``run_id``, NULL without one)."""
    s, r = tables.schedules, tables.runs
    held = (
        select(func.min(r.c.id)).where(r.c.schedule_id == s.c.id, one_of(r.c.state, QUEUED_OR_HELD)).scalar_subquery()
    )
    return select(s.c.id, s.c.project_id, s.c.paused_at, held.label("run_id")).where(
        one_of(s.c.project_id, name="projects")
    )


def _open_proposal_labels():
    """The label of each open proposal of the projects bound as ``projects``."""
    p = tables.proposals
    return select(p.c.project_id, p.c.label).where(one_of(p.c.project_id, name="projects"), p.c.state == "open")


# Built once with bind parameters: GET /v1/me/overview runs them on every request (docs/hub.md, Data access).
_NEWEST_CHARTERS = _newest_charters()
_SCHEDULES_HELD = _schedules_held()
_OPEN_PROPOSAL_LABELS = _open_proposal_labels()


async def open_proposals(conn: AsyncConnection, accesses: list[ProjectAccess]) -> dict[int, int]:
    """Per project of ``accesses``, its open proposals whose label the access reaches through the project's hub
    sink, as GET .../curator/proposals reads them."""
    by_id = {access.project_id: access for access in accesses}
    if not by_id:
        return {}
    counted: Counter[int] = Counter()
    for project_id, label in (await conn.execute(_OPEN_PROPOSAL_LABELS, {"projects": list(by_id)})).all():
        access = by_id[project_id]
        if access.visible(label, plan_routes._sink(access, None)):
            counted[project_id] += 1
    return dict(counted)


async def curator_overview(conn: AsyncConnection, accesses: list[ProjectAccess]) -> dict[int, CuratorOverview]:
    """Where the Curator of each project of ``accesses`` that has a charter stands, in three statements built once
    for all of them."""
    ids = [access.project_id for access in accesses]
    if not ids:
        return {}
    charters = {row.project_id: row for row in (await conn.execute(_NEWEST_CHARTERS, {"projects": ids})).all()}
    if not charters:
        return {}
    found = (await conn.execute(_SCHEDULES_HELD, {"projects": list(charters)})).all()
    waiting = await open_proposals(conn, [access for access in accesses if access.project_id in charters])
    states = {}
    for project_id, charter in charters.items():
        window = charter.body["window"]
        inside, _ = curator.window_state(charter.local_now, window["start"], window["end"])
        mine = [row for row in found if row.project_id == project_id]
        paused = bool(mine) and all(row.paused_at is not None for row in mine)
        run_id = min((row.run_id for row in mine if row.run_id is not None), default=None)
        states[project_id] = CuratorOverview(
            state=curator_state(paused, run_id, inside),
            in_window=inside,
            active_run_id=run_id,
            open_proposals=waiting.get(project_id, 0),
        )
    return states


async def _status(conn: AsyncConnection, access: ProjectAccess) -> CuratorStatus:
    row = await _charter_row(conn, access.project_id)
    found = (await conn.execute(_schedules(access.project_id))).all()
    schedules = [Schedule(**{name: item._mapping[name] for name in Schedule.model_fields}) for item in found]
    night = None
    if row is not None:
        window = row.body["window"]
        local_now = (await conn.execute(select(func.timezone(window["timezone"], func.now())))).scalar_one()
        inside, which = curator.window_state(local_now, window["start"], window["end"])
        duty = next((item for item in found if item.kind == NIGHT_SHIFT), None)
        figures = NightFigures(0, 0.0, None) if duty is None else await night_figures(conn, duty.id, which)
        night = Night(
            night=which,
            in_window=inside,
            local_time=local_now.strftime("%H:%M"),
            runs=figures.runs,
            max_runs=row.body["max_runs_per_night"],
            cost_usd=round(figures.cost_usd, 6),
            budget_usd=row.body["night_budget_usd"],
            active_run_id=figures.active_run_id,
        )
    paused = bool(schedules) and all(item.paused_at is not None for item in schedules)
    return CuratorStatus(
        project=access.name,
        charter=None if row is None else _charter_view(access.name, row, admin=has_role(access.role, "admin")),
        paused=paused,
        schedules=schedules,
        night=night,
        last_review_run=await _last_review(conn, access.project_id),
        state=None if night is None else curator_state(paused, night.active_run_id, night.in_window),
        open_proposals=(await open_proposals(conn, [access])).get(access.project_id, 0),
        last_brief=await last_brief(conn, access.project_id),
    )


async def last_brief(conn: AsyncConnection, project_id: int) -> BriefSummary | None:
    b, u = tables.curator_briefs, tables.users
    query = (
        select(b.c.id, b.c.day, b.c.night, b.c.created_at, u.c.login, b.c.notification_id, b.c.body["title"].astext)
        .join_from(b, u, u.c.id == b.c.user_id)
        .where(b.c.project_id == project_id)
        .order_by(b.c.day.desc())
        .limit(1)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return None
    day, night, sent_at, login, notification_id, title = row[1:]
    return BriefSummary(
        id=row.id,
        day=day,
        night=night,
        sent_at=sent_at,
        to=login,
        notification_id=notification_id,
        title=title or f"Morning brief of {day.isoformat()}",
    )


async def _last_review(conn: AsyncConnection, project_id: int) -> ReviewRunSummary | None:
    r, f, p, cf = tables.runs, tables.findings, tables.proposals, tables.curator_figures
    findings = select(func.count()).where(f.c.run_id == r.c.id).scalar_subquery()
    proposals = select(func.count()).where(p.c.run_id == r.c.id).scalar_subquery()
    lenses = select(cf.c.figures["lenses"]).where(cf.c.run_id == r.c.id).limit(1).scalar_subquery()
    query = (
        select(
            r.c.id,
            r.c.state,
            r.c.schedule_night.label("night"),
            lenses.label("lenses"),
            findings.label("findings"),
            proposals.label("proposals"),
            r.c.queued_at,
            r.c.finished_at,
        )
        .where(r.c.project_id == project_id, r.c.kind == "review")
        .order_by(r.c.id.desc())
        .limit(1)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return None
    found = dict(row._mapping)
    found["lenses"] = [name for name in found["lenses"] or [] if isinstance(name, str)]
    return ReviewRunSummary(**found)


@router.get(
    "/{project}/curator",
    response_model=CuratorStatus,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def status(request: Request, project: ProjectName, user: CurrentUser) -> CuratorStatus:
    """The charter, the schedules and the night of the project's night shift."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        return await _status(conn, access)


def _nights_of(project_id: int, limit: int):
    """The latest ``limit`` nights of the project: those its schedules queued runs for, and those curator.collect
    counted figures for."""
    r, s, cf = tables.runs, tables.schedules, tables.curator_figures
    queued = select(r.c.schedule_night.label("night")).where(
        r.c.schedule_id.in_(select(s.c.id).where(s.c.project_id == project_id)), r.c.schedule_night.is_not(None)
    )
    counted = select(cf.c.night.label("night")).where(cf.c.project_id == project_id)
    nights = union(queued, counted).subquery("nights")
    return select(nights.c.night).order_by(nights.c.night.desc()).limit(limit)


def _night_runs(project_id: int, nights: list[date]):
    """Per night of ``nights``: the runs the project's schedules queued for it, by how they stand."""
    r, s = tables.runs, tables.schedules
    return (
        select(
            r.c.schedule_night.label("night"),
            func.count().label("runs"),
            func.count().filter(r.c.state == "done").label("done"),
            func.count().filter(r.c.state.in_(("failed", "lost"))).label("failed"),
            func.count().filter(r.c.state == "cancelled").label("cancelled"),
            func.count().filter(r.c.state.in_(runs.ACTIVE_STATES)).label("active"),
        )
        .where(
            r.c.schedule_id.in_(select(s.c.id).where(s.c.project_id == project_id)),
            r.c.schedule_night.in_(nights),
        )
        .group_by(r.c.schedule_night)
    )


def _night_costs(project_id: int, nights: list[date]):
    """Per night of ``nights``: what its runs cost, each agent session once at its largest total, as
    ``_night_figures`` counts one night."""
    r, s = tables.runs, tables.schedules
    session = func.coalesce(r.c.session_id, literal("run:") + cast(r.c.id, Text))
    per_session = (
        select(r.c.schedule_night.label("night"), func.max(usage_cost(r.c.usage)).label("cost"))
        .where(
            r.c.schedule_id.in_(select(s.c.id).where(s.c.project_id == project_id)),
            r.c.schedule_night.in_(nights),
        )
        .group_by(r.c.schedule_night, session)
        .subquery("per_session")
    )
    return select(per_session.c.night, func.sum(per_session.c.cost).label("cost")).group_by(per_session.c.night)


def _night_reviews(project_id: int, nights: list[date]):
    """The review runs of the project for ``nights``, with what each wrote, the latest first."""
    r, f, p = tables.runs, tables.findings, tables.proposals
    return (
        select(
            r.c.id,
            r.c.state,
            r.c.schedule_night.label("night"),
            select(func.count()).where(f.c.run_id == r.c.id).scalar_subquery().label("findings"),
            select(func.count()).where(p.c.run_id == r.c.id).scalar_subquery().label("proposals"),
        )
        .where(r.c.project_id == project_id, r.c.kind == "review", r.c.schedule_night.in_(nights))
        .order_by(r.c.id.desc())
    )


@router.get(
    "/{project}/curator/nights",
    response_model=NightList,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def nights(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=MAX_NIGHTS, description="the latest nights")] = DEFAULT_NIGHTS,
) -> NightList:
    """The latest nights of the project's night shift, each with its runs, their cost and its review run."""
    cf = tables.curator_figures
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        charter = await _charter_row(conn, access.project_id)
        which = list((await conn.execute(_nights_of(access.project_id, limit))).scalars())
        counts, costs, reviews, counted = {}, {}, {}, set()
        if which:
            counts = {row.night: row for row in (await conn.execute(_night_runs(access.project_id, which))).all()}
            costs = {row.night: row.cost for row in (await conn.execute(_night_costs(access.project_id, which))).all()}
            for row in (await conn.execute(_night_reviews(access.project_id, which))).all():
                reviews.setdefault(row.night, row)
            figures = select(cf.c.night).where(cf.c.project_id == access.project_id, cf.c.night.in_(which))
            counted = set((await conn.execute(figures)).scalars())
    listed = []
    for night in which:
        row, review_row = counts.get(night), reviews.get(night)
        listed.append(
            NightSummary(
                night=night,
                runs=row.runs if row else 0,
                done=row.done if row else 0,
                failed=row.failed if row else 0,
                cancelled=row.cancelled if row else 0,
                active=row.active if row else 0,
                cost_usd=round(float(costs.get(night) or 0), 6),
                review_run=None
                if review_row is None
                else NightReview(
                    id=review_row.id,
                    state=review_row.state,
                    findings=review_row.findings,
                    proposals=review_row.proposals,
                ),
                figures=night in counted,
            )
        )
    return NightList(
        project=access.name,
        nights=listed,
        budget_usd=None if charter is None else charter.body["night_budget_usd"],
        max_runs=None if charter is None else charter.body["max_runs_per_night"],
    )


# Pausing


async def _pausable(conn: AsyncConnection, user: Principal, project: str, doing: str):
    """(access, ids of the project's schedules, locked) for an admin of the project or the owner of one of them."""
    access = await project_access(conn, user, project)
    _reader(access)
    s = tables.schedules
    rows = (
        await conn.execute(
            select(s.c.id, s.c.owner_id).where(s.c.project_id == access.project_id).order_by(s.c.id).with_for_update()
        )
    ).all()
    if not rows:
        raise HTTPException(409, f"project {access.name} has no schedule to {doing}: an admin writes its charter first")
    if not has_role(access.role, "admin") and all(row.owner_id != user.user_id for row in rows):
        raise HTTPException(
            403, f"{doing} the night shift of project {access.name} needs the admin role on it, or one of its schedules"
        )
    return access, [row.id for row in rows]


@router.post(
    "/{project}/curator/pause",
    response_model=CuratorStatus,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
)
async def pause(request: Request, project: ProjectName, user: CurrentUser) -> CuratorStatus:
    """Pause every schedule of the project, and cancel the runs they queued that are still queued."""
    s = tables.schedules
    async with request.app.state.engine.begin() as conn:
        access, ids = await _pausable(conn, user, project, "pause")
        await conn.execute(
            update(s)
            .values(paused_at=func.now(), paused_by=user.user_id, updated_at=func.now())
            .where(s.c.id.in_(ids), s.c.paused_at.is_(None))
        )
        cancelled = await cancel_queued(conn, ids, f"the night shift of {access.name} was paused by {user.login}")
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=PAUSE,
            target=access.name,
            project_id=access.project_id,
        )
        found = await _status(conn, access)
    log.info("night shift paused", extra={"project": project, "login": user.login, "cancelled": cancelled})
    return found


@router.post(
    "/{project}/curator/resume",
    response_model=CuratorStatus,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
)
async def resume(request: Request, project: ProjectName, user: CurrentUser) -> CuratorStatus:
    """Let every schedule of the project run again."""
    s = tables.schedules
    async with request.app.state.engine.begin() as conn:
        access, ids = await _pausable(conn, user, project, "resume")
        await conn.execute(
            update(s)
            .values(paused_at=None, paused_by=None, pause_reason=None, updated_at=func.now())
            .where(s.c.id.in_(ids), s.c.paused_at.is_not(None))
        )
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=RESUME,
            target=access.name,
            project_id=access.project_id,
        )
        found = await _status(conn, access)
    log.info("night shift resumed", extra={"project": project, "login": user.login})
    return found


# The job


def _due(now: datetime | None):
    """Each night_shift schedule of a project the Curator is on for, with its project, its owner's login, the newest
    revision of its charter, and the local time in the charter's zone at ``now`` (the database's now when None)."""
    s, c, p, u = tables.schedules, tables.charters, tables.projects, tables.users
    newest = (
        select(c.c.project_id, func.max(c.c.revision).label("revision")).group_by(c.c.project_id).subquery("newest")
    )
    moment = func.now() if now is None else literal(now)
    return (
        select(
            s.c.id,
            s.c.project_id,
            p.c.name.label("project"),
            s.c.owner_id,
            u.c.login.label("owner"),
            s.c.worker_id,
            s.c.pool_id,
            c.c.revision,
            c.c.body,
            func.timezone(c.c.body["window"]["timezone"].astext, moment).label("local_now"),
        )
        .join_from(s, p, p.c.id == s.c.project_id)
        .join(u, u.c.id == s.c.owner_id)
        .join(newest, newest.c.project_id == s.c.project_id)
        .join(c, and_(c.c.project_id == newest.c.project_id, c.c.revision == newest.c.revision))
        .where(s.c.kind == NIGHT_SHIFT, hub_settings.feature_on("curator", s.c.project_id))
        .order_by(s.c.id)
    )


async def _owner_access(conn: AsyncConnection, owner_id: int, login: str, project: str) -> ProjectAccess | None:
    """The access of the schedule's owner to the project, as a request of theirs would get it; None without one."""
    member = Principal(user_id=owner_id, login=login, admin=False, token_id=0, kind=MACHINE, token_hash="")
    try:
        return await project_access(conn, member, project)
    except HTTPException:
        return None


def _ready(body: dict) -> bool:
    steps = body.get("steps") if isinstance(body.get("steps"), list) else []
    return any(runs.unready_reason(body, step) is None for step in steps)


async def _next_plan(conn: AsyncConnection, access: ProjectAccess, charter: dict, duty: Duty):
    """(plan held, its repos) of the first plan of the charter's night_plans the owner may read, with a ready step,
    no active run, and repos a worker on duty has; the plan's dispatch lock is taken. (None, why) when there is none."""
    runtime = charter["builder"]["runtime"]
    skipped = []
    for plan_id in charter["night_plans"]:
        held = await plan_routes._held(conn, access, plan_id)
        if held is None or not access.visible(held.label, plan_routes._sink(access, None)):
            skipped.append(f"{plan_id}: not on the hub")
            continue
        await lock_plan(conn, access.project_id, plan_id)
        activity = await plan_activity(conn, access.project_id, plan_id)
        if activity.plan_run is not None or activity.steps:
            skipped.append(f"{plan_id}: has an active run")
            continue
        if not _ready(held.body):
            skipped.append(f"{plan_id}: no ready step")
            continue
        try:
            repos = plan_run_repos(held)
        except HTTPException as exc:
            skipped.append(f"{plan_id}: {exc.detail}")
            continue
        problems = duty.unfit(access.name, "plan", [entry["repo"] for entry in repos], runtime)
        if problems:
            skipped.append(f"{plan_id}: {'; '.join(problems)}")
            continue
        return (held, repos), None
    return None, "; ".join(skipped) or "the charter names no plan"


async def _queue(conn: AsyncConnection, due, night: date, duty: Duty, plan, budget: dict) -> int:
    """Queue the plan run of ``plan`` (held, repos) for schedule ``due`` and ``night``, placed on ``duty``, with
    ``budget``; its id. The insert's IntegrityError goes on to the caller, whose transaction stays usable."""
    held, repos = plan
    builder = due.body["builder"]
    values = {
        "kind": "plan",
        "project_id": due.project_id,
        "plan_id": held.plan_id,
        "title": runs.step_title(held.body),
        "plan_revision": held.revision,
        "dispatched_by": due.owner_id,
        "dispatched_via": "schedule",
        **duty.columns(),
        "requested_runtime": builder["runtime"],
        "runtime": builder["runtime"],
        "model": builder.get("model"),
        "mode": "headless",
        "approval": "auto",
        "timeout_s": budget["max_seconds"] + curator.BUDGET_GRACE_SECONDS,
        "repos": repos,
        "schedule_id": due.id,
        "schedule_night": night,
        "budget": budget,
    }
    run_id = await insert_run(conn, values)
    caps = (
        f"cost cap {curator.money(budget['max_usd'])}, {budget['max_turns']} turns, "
        f"{budget['max_seconds'] // 60} minutes of agent time"
    )
    text = (
        f"Queued by the night shift of project {due.project} (charter revision {due.revision}) for the night of "
        f"{night.isoformat()}, as {due.owner} on {duty.where}: {caps}."
    )
    await write_event(conn, run_id, {"text": text, "schedule_id": due.id, "night": night.isoformat(), **budget})
    await notify_queued(conn, run_id)
    await audit.record(
        conn,
        actor_id=due.owner_id,
        token_id=None,
        action=DISPATCH,
        target=f"{due.project}/{held.plan_id} run:{run_id} schedule:{due.id} night={night.isoformat()}",
        project_id=due.project_id,
    )
    return run_id


async def _writer_access(conn: AsyncConnection, due) -> ProjectAccess | None:
    access = await _owner_access(conn, due.owner_id, due.owner, due.project)
    if access is None or not has_role(access.role, "writer"):
        log.warning("night shift skipped: its owner holds no writer grant", extra={"project": due.project})
        return None
    return access


async def gate_of(conn: AsyncConnection, due, *, figures_first=False, any_hour=False, beside=False, on_demand=False):
    """(word, gate) for schedule ``due`` now, in the caller's transaction, under the schedule's row lock it takes: a
    ``collect.Gate`` when the night shift may queue a run now, else None and the word for the job's summary. Paused,
    the schedule's queued runs are cancelled; outside its window, those of the night alone (``shift_hours``), and with
    ``any_hour`` (the job hub.fire_schedules) the gate is for the Curator's changes, ``inside`` false; ``beside``
    (``build_now``) opens it though another run of the schedule is in flight. With
    ``figures_first`` (the job curator.collect), the night's figures are counted as soon as the window is open, before
    a run of the schedule held now ends. With ``on_demand`` (a KG optimize run asked for, ``kg_optimize``) the window
    is not asked and nothing is cancelled."""
    from evo_agents.hub.server.collect import Gate, ensure_figures  # it queues runs through this module
    from evo_agents.hub.server.outcomes import check_circuit  # it pauses through this module

    s = tables.schedules
    locked = select(s.c.paused_at, s.c.updated_at).where(s.c.id == due.id).with_for_update(skip_locked=True)
    found = (await conn.execute(locked)).one_or_none()
    if found is None:
        return "locked", None
    window = due.body["window"]
    inside, night = curator.window_state(due.local_now, window["start"], window["end"])
    if found.paused_at is None and await check_circuit(conn, due, night, found.updated_at):
        return "circuit", None
    if found.paused_at is not None:
        cancelled = await cancel_queued(conn, [due.id], f"the night shift of {due.project} is paused")
        return ("cancelled" if cancelled else "paused"), None
    if not inside and not on_demand:
        why = f"the night shift of {due.project} is outside its window"
        cancelled = await cancel_queued(conn, [due.id], why, changes_too=False)
        if (cancelled and not beside) or not any_hour or not await changes_waiting(conn, due.project_id):
            return ("cancelled" if cancelled else "outside"), None
    access = None
    if figures_first:
        access = await _writer_access(conn, due)
        if access is None:
            return "owner", None
        async with conn.begin_nested():
            await ensure_figures(conn, due, night, access)
    figures = await night_figures(conn, due.id, night)
    if figures.active_run_id is not None and not beside:
        return "busy", None
    if figures.runs >= due.body["max_runs_per_night"]:
        return "max_runs", None
    budget = next_run_budget(due.body, figures)
    if budget is None:
        return "spent", None
    access = access or await _writer_access(conn, due)
    if access is None:
        return "owner", None
    duty, why = await on_duty(conn, due)
    if duty is None:
        word = "pool" if due.pool_id is not None else "worker"
        log.info(f"night shift waits for its {word}", extra={"project": due.project, "why": why})
        return word, None
    return "", Gate(night=night, figures=figures, budget=budget, access=access, duty=duty, inside=inside)


async def project_gate(conn: AsyncConnection, project_id: int):
    """(the project's night_shift schedule, the word, the gate) for a Builder a member asks for now: ``gate_of`` at
    any hour and beside the schedule's other runs; (None, "none", None) for a project without one."""
    due = (await conn.execute(_due(None).where(tables.schedules.c.project_id == project_id))).one_or_none()
    if due is None:
        return None, "none", None
    word, gate = await gate_of(conn, due, any_hour=True, beside=True)
    return due, word, gate


async def _fire(conn: AsyncConnection, due, *, curator_app: bool) -> str:
    """What schedule ``due`` does now, in the caller's transaction; a word for the job's summary."""
    from evo_agents.hub.server.changes import queue_builder, queue_judge  # it queues runs through this module
    from evo_agents.hub.server.collect import queue_review  # it queues runs through this module
    from evo_agents.hub.server.history import queue_history  # it queues runs through this module
    from evo_agents.hub.server.probe_runs import queue_probe  # it queues runs through this module
    from evo_agents.hub.server.replays import queue_replay  # it queues runs through this module

    word, gate = await gate_of(conn, due, any_hour=True)
    if gate is None:
        return word
    if gate.inside and await queue_review(conn, due, gate) is not None:
        return "review"
    if gate.inside and await queue_history(conn, due, gate) is not None:
        return "history"
    if await queue_judge(conn, due, gate) is not None:
        return "judge"
    if await queue_builder(conn, due, gate, curator_app=curator_app) is not None:
        return "builder"
    if await queue_replay(conn, due, gate) is not None:
        return "replay"
    if not gate.inside:  # the night's plans wait for the window
        return "outside"
    night, duty, budget = gate.night, gate.duty, gate.budget
    plan, why = await _next_plan(conn, gate.access, due.body, duty)
    if plan is None:
        if await queue_probe(conn, due, gate) is not None:  # the probes queue after every other kind of the night
            return "probe"
        log.info("night shift has no plan to run", extra={"project": due.project, "why": why})
        return "no_plan"
    try:
        run_id = await _queue(conn, due, night, duty, plan, budget)
    except IntegrityError:  # a dispatch of the plan got in first, though the plan's lock is held: try next minute
        return "no_plan"
    log.info(
        "night shift queued a run",
        extra={"project": due.project, "plan_id": plan[0].plan_id, "run_id": run_id, "night": night.isoformat()},
    )
    return "queued"


async def fire_schedules(engine: AsyncEngine, *, curator_app: bool, now: datetime | None = None) -> dict:
    """One pass of ``hub.fire_schedules`` over each night_shift schedule, in a transaction of its own, at ``now`` (the
    database's now when None), ``curator_app`` whether this hub has the Curator's App; how many ended each outcome."""
    async with engine.begin() as conn:
        due = (await conn.execute(_due(now))).all()
    outcomes: Counter = Counter()
    for row in due:
        try:
            async with engine.begin() as conn:
                outcomes[await _fire(conn, row, curator_app=curator_app)] += 1
        except Exception:  # one schedule failing leaves the others their turn
            log.exception("a schedule failed", extra={"project": row.project, "schedule_id": row.id})
            outcomes["failed"] += 1
    return dict(sorted(outcomes.items()))
