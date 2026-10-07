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
from sqlalchemy.dialects.postgresql import psycopg as pg_psycopg

from evo_agents.hub import runs, tables
from evo_agents.hub.db import legacy
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plans import step_counts
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

TODAY = "SELECT (now() AT TIME ZONE 'UTC')::date"
GRANTED = """
SELECT p.name FROM grants g JOIN projects p ON p.id = g.project_id WHERE g.user_id = %s ORDER BY p.name
"""
VISIBLE = """
WITH visible (project_id, plan_id) AS (SELECT * FROM unnest(%(projects)s::bigint[], %(plans)s::text[]))
"""
RUN_COUNTS = (
    VISIBLE
    + """
SELECT r.state, (r.finished_at AT TIME ZONE 'UTC')::date, count(*)
  FROM runs r JOIN visible v ON v.project_id = r.project_id AND v.plan_id = r.plan_id
 WHERE r.state = ANY(%(in_flight)s) OR (r.state = ANY(%(ended)s) AND r.finished_at >= %(since)s)
 GROUP BY 1, 2
"""
)
ACTIVE_ORDER = (
    "CASE WHEN r.state = ANY(%(running)s) THEN 0 ELSE array_position(%(states)s::text[], r.state) END, r.id DESC"
)
RECENT_ORDER = "r.finished_at DESC, r.id DESC"


def _step_counts_text() -> str:
    """plans.step_counts over pl.body, as the text the string queries below interpolate: a bridge until this module's
    queries are Core and select step_counts itself."""
    body = tables.plans.alias("pl").c.body
    dialect = pg_psycopg.dialect()
    return ", ".join(str(c.compile(dialect=dialect, compile_kwargs={"literal_binds": True})) for c in step_counts(body))


def _run_rows(order: str) -> str:
    """The first runs by ``order`` in the states named, of the visible plans: picked from runs alone, so the joins
    that name their project, owner, worker and plan run for those few only."""
    return (
        VISIBLE
        + f""", picked AS (
    SELECT r.id FROM runs r JOIN visible v ON v.project_id = r.project_id AND v.plan_id = r.plan_id
     WHERE r.state = ANY(%(states)s)
     ORDER BY {order}
     LIMIT %(limit)s
)
SELECT r.id, r.kind, p.name, r.plan_id, pl.body ->> 'title', r.step_key, r.title, r.state, u.login, r.worker_id,
       w.name, r.runtime, r.model, {_step_counts_text()}, r.run_seconds, r.queued_at, r.started_at,
       r.finished_at, r.error
  FROM picked JOIN runs r ON r.id = picked.id
  JOIN projects p ON p.id = r.project_id JOIN users u ON u.id = r.dispatched_by
  LEFT JOIN workers w ON w.id = r.worker_id
  LEFT JOIN plans pl ON pl.project_id = r.project_id AND pl.plan_id = r.plan_id
 ORDER BY {order}
"""
    )


ACTIVE_RUNS = _run_rows(ACTIVE_ORDER)
RECENT_RUNS = _run_rows(RECENT_ORDER)
OPEN_DECISIONS = (
    VISIBLE
    + """
SELECT d.id, p.name, d.run_id, r.state, d.plan_id, pl.body ->> 'title', d.step_key, d.category, d.question, o.login,
       r.dispatched_by = %(user)s, d.asked_at, r.waiting_since, r.parked_at
  FROM decisions d JOIN visible v ON v.project_id = d.project_id AND v.plan_id = d.plan_id
  JOIN runs r ON r.id = d.run_id JOIN projects p ON p.id = d.project_id JOIN users o ON o.id = r.dispatched_by
  LEFT JOIN plans pl ON pl.project_id = d.project_id AND pl.plan_id = d.plan_id
 WHERE d.state = 'open'
 ORDER BY r.dispatched_by = %(user)s DESC, d.asked_at, d.id
 LIMIT %(limit)s
"""
)
DECISION_COUNTS = (
    VISIBLE
    + """
SELECT d.project_id, count(*), count(*) FILTER (WHERE r.dispatched_by = %(user)s)
  FROM decisions d JOIN visible v ON v.project_id = d.project_id AND v.plan_id = d.plan_id
  JOIN runs r ON r.id = d.run_id
 WHERE d.state = 'open'
 GROUP BY d.project_id
"""
)
PROJECT_COUNTS = (
    VISIBLE
    + """
SELECT p.id,
       (SELECT count(*) FROM project_repos pr WHERE pr.project_id = p.id),
       (SELECT count(*) FROM plans pl JOIN visible v ON v.project_id = pl.project_id AND v.plan_id = pl.plan_id
         WHERE pl.project_id = p.id AND pl.area = 'active')
  FROM projects p
 WHERE p.id = ANY(%(granted)s::bigint[])
"""
)


async def _granted(conn, user) -> list[ProjectAccess]:
    """The caller's access to each project it holds a grant on, by the check of the project's runs and decisions; a
    grant revoked since the list was read leaves its project out."""
    names = [row[0] for row in await (await legacy(conn, GRANTED, (user.user_id,))).fetchall()]
    accesses = []
    for name in names:
        try:
            accesses.append(await project_access(conn, user, name))
        except HTTPException:
            continue
    return [access for access in accesses if access.role is not None]


def _run(row) -> OverviewRun:
    fields = dict(zip(OverviewRun.model_fields, row, strict=True))
    if fields["kind"] != "plan":
        fields["steps_done"] = fields["steps_total"] = None
    return OverviewRun(**fields)


def _parks_at(state: str, waiting_since: datetime | None, parked_at: datetime | None, wait: timedelta):
    if state == "waiting" and waiting_since is not None:
        return waiting_since + wait
    return parked_at if state == "parked" else None


def _decision(row, wait: timedelta) -> OverviewDecision:
    *shown, asked_at, waiting_since, parked_at = row
    names = [name for name in OverviewDecision.model_fields if name not in ("asked_at", "parks_at")]
    fields = dict(zip(names, shown, strict=True))
    return OverviewDecision(
        **fields, asked_at=asked_at, parks_at=_parks_at(fields["run_state"], waiting_since, parked_at, wait)
    )


@router.get("/overview", response_model=Overview)
async def overview(request: Request, user: CurrentUser) -> Overview:
    """What waits for you, what runs and what ended lately, over the projects you hold a grant on."""
    wait = timedelta(seconds=request.app.state.config.decision_wait_seconds)
    async with request.app.state.engine.begin() as conn:
        today: date = (await (await legacy(conn, TODAY)).fetchone())[0]
        accesses = await _granted(conn, user)
        pairs = []
        for access in accesses:
            pairs += [(access.project_id, plan_id) for plan_id in await visible_plans(conn, access, None)]
        since = datetime.combine(today - timedelta(days=DAYS - 1), time.min, tzinfo=UTC)
        params = {
            "projects": [project_id for project_id, _ in pairs],
            "plans": [plan for _, plan in pairs],
            "granted": [access.project_id for access in accesses],
            "user": user.user_id,
            "in_flight": list(IN_FLIGHT),
            "running": list(RUNNING_STATES),
            "ended": list(ENDED_COUNTED),
            "since": since,
        }
        counted = await (await legacy(conn, RUN_COUNTS, params)).fetchall()
        in_flight = params | {"states": list(IN_FLIGHT), "limit": MAX_ACTIVE}
        active = await (await legacy(conn, ACTIVE_RUNS, in_flight)).fetchall()
        ended = params | {"states": list(runs.TERMINAL_STATES), "limit": MAX_RECENT}
        recent = await (await legacy(conn, RECENT_RUNS, ended)).fetchall()
        decisions = await (await legacy(conn, OPEN_DECISIONS, params | {"limit": MAX_DECISIONS})).fetchall()
        decision_counts = await (await legacy(conn, DECISION_COUNTS, params)).fetchall()
        project_counts = await (await legacy(conn, PROJECT_COUNTS, params)).fetchall()

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
