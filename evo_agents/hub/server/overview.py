"""The caller's overview, for the web's Home: GET /v1/me/overview, with a web session or a machine token.

One answer says what waits for the caller, what runs and what ended lately, over every project the caller holds a
grant on; a hub admin's projects without one are left out. Each project goes through the check of GET
/v1/projects/{p}/runs and GET /v1/projects/{p}/decisions, the same functions (``projects.project_access`` and
``runs.visible_plans``, through the project's hub sink), so a run or decision of a plan whose label the caller's grant
does not reach is neither listed nor counted. The queries then take the visible (project, plan) pairs at once, and
use the indexes the runs, decisions and plans already have.

- counts: waiting_on_you, the open decisions of runs the caller dispatched; running, the runs in RUNNING_STATES;
  queued; and the runs that ended done, failed or lost on the last DAYS days in UTC, today included;
- done_by_day: the runs that ended done on each of those days, oldest first, so its sum is counts.done_7d;
- active_runs: at most MAX_ACTIVE runs in IN_FLIGHT, those whose agent works first, then waiting, parked, queued,
  the newest first within each;
- recent_runs: the MAX_RECENT runs that ended last, in any end state;
- open_decisions: at most MAX_DECISIONS open decisions, the caller's own first, the oldest first, each with when its
  run parks for want of an answer (EVO_HUB_DECISION_WAIT_SECONDS after it started waiting);
- projects: each with the caller's role and max level, its repos, its active plans the caller sees and the open
  decisions of those plans.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Date, Text, and_, case, cast, column, func, literal, or_, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.runs import REQUESTED_RUNTIMES, visible_plans
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

DAYS = 7  # the days of done_by_day and of the *_7d counts, today in UTC the last of them
MAX_ACTIVE = 20
MAX_RECENT = 10
MAX_DECISIONS = 20
RUNNING_STATES = runs.CLOCK_STATES  # an agent at work, headless or driven by a person, or its verify commands
IN_FLIGHT = (*RUNNING_STATES, "waiting", "parked", "queued")  # in the order active_runs shows them
ENDED_COUNTED = ("done", "failed", "lost")  # the end states counts has a number for; a cancel is the owner's own

router = APIRouter(prefix="/v1/me", tags=["overview"], responses={401: {"model": ErrorBody}})


# Models


class OverviewCounts(BaseModel):
    waiting_on_you: int = Field(description="open decisions of the runs you dispatched, which only you may answer")
    running: int = Field(description=f"runs whose agent works: {', '.join(RUNNING_STATES)}")
    queued: int = Field(description="runs no worker has claimed yet")
    done_7d: int = Field(description=f"runs that ended done in the last {DAYS} days in UTC, today included")
    failed_7d: int = Field(description="runs that ended failed in the same days")
    lost_7d: int = Field(description="runs whose worker stopped extending the lease, in the same days")


class DayCount(BaseModel):
    day: date = Field(description="a day in UTC")
    done: int = Field(description="runs that ended done that day")


class OverviewRun(BaseModel):
    id: int
    kind: Literal[runs.RUN_KINDS]
    project: str
    plan_id: str
    plan_title: str | None = Field(description="the plan's title as the hub holds it now")
    step_key: str | None = Field(description="null for a plan run")
    title: str | None = Field(description="the step's title when the run was dispatched; the plan's for a plan run")
    state: Literal[runs.RUN_STATES]
    dispatched_by: str = Field(description="the login of the member who dispatched it, its owner")
    worker_id: int | None = Field(description="the worker that claimed it; null while it is queued")
    worker: str | None = Field(description="that worker's name")
    runtime: Literal[REQUESTED_RUNTIMES] = Field(description="any until a worker claims the run")
    model: str | None
    steps_total: int | None = Field(description="the steps of a plan run's plan; null for a run of one step")
    steps_done: int | None = Field(description="a plan run's steps the plan holds done now; null for a run of one step")
    run_seconds: int = Field(description="the agent time the run has used, as the hub last counted it")
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None = Field(description="why the run failed, was lost or ended")


class OverviewDecision(BaseModel):
    id: int
    project: str
    run_id: int = Field(description="the plan run the decision belongs to")
    run_state: Literal[runs.RUN_STATES]
    plan_id: str
    plan_title: str | None
    step_key: str | None = Field(description="the step it is about, when the agent named one")
    category: Literal[runs.DECISION_CATEGORIES]
    question: str
    owner: str = Field(description="the login of the run's owner, the one member who may answer")
    yours: bool = Field(description="whether you are that owner")
    asked_at: datetime
    parks_at: datetime | None = Field(
        description="when the run parks for want of an answer: EVO_HUB_DECISION_WAIT_SECONDS after it started "
        "waiting, or when it parked; null while its agent still works"
    )


class OverviewProject(BaseModel):
    name: str
    role: str = Field(description="your role on the project")
    max_level: str = Field(description="the highest label level your grant reaches")
    repos: int = Field(description="the repos registered for the project")
    active_plans: int = Field(description="its plans in the active area that you can see")
    open_decisions: int = Field(description="open decisions of the plans you can see, yours or not")


class Overview(BaseModel):
    counts: OverviewCounts
    done_by_day: list[DayCount] = Field(
        min_length=DAYS, max_length=DAYS, description=f"the last {DAYS} days in UTC, oldest first, today last"
    )
    active_runs: list[OverviewRun] = Field(
        max_length=MAX_ACTIVE,
        description=f"at most {MAX_ACTIVE} runs in {', '.join(IN_FLIGHT)}: those whose agent works first, then "
        "waiting, parked and queued, the newest first within each",
    )
    recent_runs: list[OverviewRun] = Field(
        max_length=MAX_RECENT, description=f"the {MAX_RECENT} runs that ended last, the last first"
    )
    open_decisions: list[OverviewDecision] = Field(
        max_length=MAX_DECISIONS, description=f"at most {MAX_DECISIONS}: yours first, then the oldest first"
    )
    projects: list[OverviewProject] = Field(description="the projects you hold a grant on, by name")


# Queries


def _utc_day(moment):
    """The day in UTC of the timestamp ``moment``, whatever the session's time zone."""
    return cast(func.timezone("UTC", moment), Date)


def _visible(pairs: list[tuple[int, str]]):
    """The CTE visible: the (project_id, plan_id) pairs the caller sees."""
    pair = func.unnest(
        literal([project_id for project_id, _ in pairs], ARRAY(BigInteger)),
        literal([plan_id for _, plan_id in pairs], ARRAY(Text)),
    ).table_valued(column("project_id", BigInteger), column("plan_id", Text))
    found = pair.render_derived()
    return select(found.c.project_id, found.c.plan_id).cte("visible")


def _of_visible(visible, table):
    """``table``'s rows of a visible plan: its project_id and plan_id are a pair of ``visible``."""
    return and_(visible.c.project_id == table.c.project_id, visible.c.plan_id == table.c.plan_id)


def _step_counts(body) -> tuple:
    """Two columns over the JSONB plan body ``body``, labelled steps_total and steps_done: how many steps it has, and
    how many of them are done, as GET .../plans counts them (zero when its steps are not an array; a step counts as
    done when it is an object with status done)."""
    steps = body["steps"]
    listed = func.jsonb_typeof(steps) == "array"
    step = func.jsonb_array_elements(steps, type_=JSONB).column_valued("s")
    done = (
        select(func.count())
        .where(func.jsonb_typeof(step) == "object", step["status"].astext == "done")
        .scalar_subquery()
    )
    total = case((listed, func.jsonb_array_length(steps)), else_=0)
    return total.label("steps_total"), case((listed, done), else_=0).label("steps_done")


def _granted(user_id: int):
    """The names of the projects member ``user_id`` holds a grant on, by name."""
    grants, projects = tables.grants, tables.projects
    return (
        select(projects.c.name)
        .join_from(grants, projects, projects.c.id == grants.c.project_id)
        .where(grants.c.user_id == user_id)
        .order_by(projects.c.name)
    )


def _run_counts(visible, since: datetime):
    """The visible runs in flight, and those that ended counted since ``since``, by state and UTC day of their end."""
    runs_ = tables.runs
    day = _utc_day(runs_.c.finished_at)
    return (
        select(runs_.c.state, day.label("day"), func.count().label("count"))
        .join_from(runs_, visible, _of_visible(visible, runs_))
        .where(
            or_(
                runs_.c.state.in_(IN_FLIGHT),
                and_(runs_.c.state.in_(ENDED_COUNTED), runs_.c.finished_at >= since),
            )
        )
        .group_by(runs_.c.state, day)
    )


def _active_order() -> tuple:
    """The agents at work first, then IN_FLIGHT's order; the newest first within each."""
    runs_ = tables.runs
    rank = case(
        (runs_.c.state.in_(RUNNING_STATES), 0),
        else_=func.array_position(literal(list(IN_FLIGHT), ARRAY(Text)), runs_.c.state),
    )
    return rank, runs_.c.id.desc()


def _recent_order() -> tuple:
    runs_ = tables.runs
    return runs_.c.finished_at.desc(), runs_.c.id.desc()


def _run_rows(visible, states: tuple[str, ...], order: tuple, limit: int):
    """The first ``limit`` runs by ``order`` in ``states``, of the visible plans, with OverviewRun's columns: picked
    from runs alone, so the joins that name their project, owner, worker and plan run for those few only."""
    runs_, projects, users, workers, plans = tables.runs, tables.projects, tables.users, tables.workers, tables.plans
    picked = (
        select(runs_.c.id)
        .join_from(runs_, visible, _of_visible(visible, runs_))
        .where(runs_.c.state.in_(states))
        .order_by(*order)
        .limit(limit)
        .cte("picked")
    )
    steps_total, steps_done = _step_counts(plans.c.body)
    return (
        select(
            runs_.c.id,
            runs_.c.kind,
            projects.c.name.label("project"),
            runs_.c.plan_id,
            plans.c.body["title"].astext.label("plan_title"),
            runs_.c.step_key,
            runs_.c.title,
            runs_.c.state,
            users.c.login.label("dispatched_by"),
            runs_.c.worker_id,
            workers.c.name.label("worker"),
            runs_.c.runtime,
            runs_.c.model,
            steps_total,
            steps_done,
            runs_.c.run_seconds,
            runs_.c.queued_at,
            runs_.c.started_at,
            runs_.c.finished_at,
            runs_.c.error,
        )
        .select_from(
            picked.join(runs_, runs_.c.id == picked.c.id)
            .join(projects, projects.c.id == runs_.c.project_id)
            .join(users, users.c.id == runs_.c.dispatched_by)
            .outerjoin(workers, workers.c.id == runs_.c.worker_id)
            .outerjoin(plans, and_(plans.c.project_id == runs_.c.project_id, plans.c.plan_id == runs_.c.plan_id))
        )
        .order_by(*order)
    )


def _open_decisions(visible, user_id: int):
    """The first MAX_DECISIONS open decisions of the visible plans, ``user_id``'s own first, then the oldest first,
    with OverviewDecision's columns and what its parks_at is made of."""
    decisions, runs_, projects, users, plans = (
        tables.decisions,
        tables.runs,
        tables.projects,
        tables.users,
        tables.plans,
    )
    yours = runs_.c.dispatched_by == user_id
    return (
        select(
            decisions.c.id,
            projects.c.name.label("project"),
            decisions.c.run_id,
            runs_.c.state.label("run_state"),
            decisions.c.plan_id,
            plans.c.body["title"].astext.label("plan_title"),
            decisions.c.step_key,
            decisions.c.category,
            decisions.c.question,
            users.c.login.label("owner"),
            yours.label("yours"),
            decisions.c.asked_at,
            runs_.c.waiting_since,
            runs_.c.parked_at,
        )
        .select_from(
            decisions.join(visible, _of_visible(visible, decisions))
            .join(runs_, runs_.c.id == decisions.c.run_id)
            .join(projects, projects.c.id == decisions.c.project_id)
            .join(users, users.c.id == runs_.c.dispatched_by)
            .outerjoin(
                plans, and_(plans.c.project_id == decisions.c.project_id, plans.c.plan_id == decisions.c.plan_id)
            )
        )
        .where(decisions.c.state == "open")
        .order_by(yours.desc(), decisions.c.asked_at, decisions.c.id)
        .limit(MAX_DECISIONS)
    )


def _decision_counts(visible, user_id: int):
    """For each project, its open decisions of the visible plans, and how many of those are ``user_id``'s."""
    decisions, runs_ = tables.decisions, tables.runs
    return (
        select(
            decisions.c.project_id,
            func.count().label("total"),
            func.count().filter(runs_.c.dispatched_by == user_id).label("yours"),
        )
        .select_from(
            decisions.join(visible, _of_visible(visible, decisions)).join(runs_, runs_.c.id == decisions.c.run_id)
        )
        .where(decisions.c.state == "open")
        .group_by(decisions.c.project_id)
    )


def _project_counts(visible, granted: list[int]):
    """For each project of ``granted``, its repos and its active plans that are visible."""
    projects, repos, plans = tables.projects, tables.project_repos, tables.plans
    repo_count = select(func.count()).where(repos.c.project_id == projects.c.id).scalar_subquery()
    plan_count = (
        select(func.count())
        .select_from(plans.join(visible, _of_visible(visible, plans)))
        .where(plans.c.project_id == projects.c.id, plans.c.area == "active")
        .scalar_subquery()
    )
    return select(projects.c.id, repo_count.label("repos"), plan_count.label("plans")).where(projects.c.id.in_(granted))


async def _granted_access(conn: AsyncConnection, user) -> list[ProjectAccess]:
    """The caller's access to each project it holds a grant on, by the check of the project's runs and decisions; a
    grant revoked since the list was read leaves its project out."""
    names = (await conn.execute(_granted(user.user_id))).scalars().all()
    accesses = []
    for name in names:
        try:
            accesses.append(await project_access(conn, user, name))
        except HTTPException:
            continue
    return [access for access in accesses if access.role is not None]


def _run(row) -> OverviewRun:
    fields = dict(row._mapping)
    if fields["kind"] != "plan":
        fields["steps_done"] = fields["steps_total"] = None
    return OverviewRun(**fields)


def _parks_at(state: str, waiting_since: datetime | None, parked_at: datetime | None, wait: timedelta):
    if state == "waiting" and waiting_since is not None:
        return waiting_since + wait
    return parked_at if state == "parked" else None


def _decision(row, wait: timedelta) -> OverviewDecision:
    fields = dict(row._mapping)
    waiting_since, parked_at = fields.pop("waiting_since"), fields.pop("parked_at")
    return OverviewDecision(**fields, parks_at=_parks_at(fields["run_state"], waiting_since, parked_at, wait))


@router.get("/overview", response_model=Overview)
async def overview(request: Request, user: CurrentUser) -> Overview:
    """What waits for you, what runs and what ended lately, over the projects you hold a grant on."""
    wait = timedelta(seconds=request.app.state.config.decision_wait_seconds)
    async with request.app.state.engine.begin() as conn:
        today: date = (await conn.execute(select(_utc_day(func.now())))).scalar_one()
        accesses = await _granted_access(conn, user)
        pairs = []
        for access in accesses:
            pairs += [(access.project_id, plan_id) for plan_id in await visible_plans(conn, access, None)]
        since = datetime.combine(today - timedelta(days=DAYS - 1), time.min, tzinfo=UTC)
        visible = _visible(pairs)
        counted = (await conn.execute(_run_counts(visible, since))).all()
        active_rows = _run_rows(visible, IN_FLIGHT, _active_order(), MAX_ACTIVE)
        active = (await conn.execute(active_rows)).all()
        recent_rows = _run_rows(visible, runs.TERMINAL_STATES, _recent_order(), MAX_RECENT)
        recent = (await conn.execute(recent_rows)).all()
        decisions = (await conn.execute(_open_decisions(visible, user.user_id))).all()
        decision_counts = (await conn.execute(_decision_counts(visible, user.user_id))).all()
        granted = [access.project_id for access in accesses]
        project_counts = (await conn.execute(_project_counts(visible, granted))).all()

    by_state: dict[str, int] = {}
    done_on: dict[date, int] = {}
    for state, day, count in counted:
        by_state[state] = by_state.get(state, 0) + count
        if state == "done":
            done_on[day] = count
    open_by_project = {project_id: total for project_id, total, _ in decision_counts}
    repos_and_plans = {project_id: (repos, plans) for project_id, repos, plans in project_counts}
    counts = OverviewCounts(
        waiting_on_you=sum(yours for _, _, yours in decision_counts),
        running=sum(by_state.get(state, 0) for state in RUNNING_STATES),
        queued=by_state.get("queued", 0),
        done_7d=by_state.get("done", 0),
        failed_7d=by_state.get("failed", 0),
        lost_7d=by_state.get("lost", 0),
    )
    days = [today - timedelta(days=back) for back in range(DAYS - 1, -1, -1)]
    projects = [
        OverviewProject(
            name=access.name,
            role=access.role,
            max_level=access.max_level,
            repos=repos_and_plans.get(access.project_id, (0, 0))[0],
            active_plans=repos_and_plans.get(access.project_id, (0, 0))[1],
            open_decisions=open_by_project.get(access.project_id, 0),
        )
        for access in accesses
    ]
    log.debug("overview", extra={"login": user.login, "projects": len(projects), "plans": len(pairs)})
    return Overview(
        counts=counts,
        done_by_day=[DayCount(day=day, done=done_on.get(day, 0)) for day in days],
        active_runs=[_run(row) for row in active],
        recent_runs=[_run(row) for row in recent],
        open_decisions=[_decision(row, wait) for row in decisions],
        projects=projects,
    )
