"""Reading runs: a run as Run shows it, the active runs of a plan and its dispatch lock, the plans whose runs a
caller may read, and the steps of a plan a dispatch would take now (GET .../plans/{plan}/ready-steps)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import and_, bindparam, case, exists, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.plans import step_key
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.runs.models import ActiveRun, ReadySteps, Run, SteadyWait, StepReadiness
from evo_agents.hub.server.security import Principal

STEADY = timedelta(seconds=runs.STEADY_SECONDS)
STEADY_GAP = timedelta(seconds=runs.STEADY_GAP_SECONDS)


def beating(w):
    """Whether worker ``w`` (the workers table or an alias of it) sends heartbeats now: its last one came at most
    STEADY_GAP ago."""
    return w.c.last_heartbeat_at >= func.now() - STEADY_GAP


def steady(w):
    """Whether worker ``w`` is steady now: it sends heartbeats, and has sent them for STEADY with none late. NULL
    (not steady) before its first heartbeat."""
    return and_(beating(w), w.c.steady_since <= func.now() - STEADY)


def lost_here(worker_id):
    """Whether the run before a run (``parent_run_id``) was lost on worker ``worker_id``, a column or a value."""
    r, parent = tables.runs, tables.runs.alias("lost_parent")
    return exists().where(parent.c.id == r.c.parent_run_id, parent.c.state == "lost", parent.c.worker_id == worker_id)


def run_select():
    """The fields of Run, each column labelled as its field: a run with the name of its project, the login of its
    owner and the name of the worker that claimed it; and for a queued attempt after a run lost on a worker that is
    not steady, that worker, as ``steady_wait_*`` (``runs_of`` makes the field of them)."""
    r, p, u, w = tables.runs, tables.projects, tables.users, tables.workers
    parent, lost_on = tables.runs.alias("lost_parent"), tables.workers.alias("lost_on")
    return (
        select(
            r.c.id,
            r.c.kind,
            p.c.name.label("project"),
            func.coalesce(r.c.plan_id, "").label("plan_id"),
            r.c.step_key,
            r.c.title,
            r.c.plan_revision,
            u.c.login.label("dispatched_by"),
            r.c.dispatched_via,
            r.c.worker_id,
            w.c.name.label("worker"),
            r.c.pinned_worker_id,
            r.c.requested_runtime,
            r.c.runtime,
            r.c.model,
            r.c.mode,
            r.c.approval,
            (r.c.timeout_s // 60).label("timeout_min"),
            r.c.run_seconds,
            r.c.attempt,
            r.c.max_attempts,
            r.c.parent_run_id,
            r.c.resume_of_run_id,
            r.c.state,
            r.c.lease_expires_at,
            r.c.session_id,
            r.c.repo,
            r.c.branch,
            r.c.repos,
            r.c.commit_sha,
            r.c.diffstat,
            r.c.verify,
            r.c.evidence,
            r.c.usage,
            r.c.budget,
            r.c.error,
            r.c.log_sha256,
            r.c.diff_sha256,
            r.c.event_seq.label("last_seq"),
            r.c.cancel_requested_at,
            r.c.takeover_requested_at,
            r.c.handback_requested_at,
            r.c.queued_at,
            r.c.leased_at,
            r.c.started_at,
            r.c.waiting_since,
            r.c.parked_at,
            r.c.finished_at,
            r.c.request,
            r.c.finish_requested_at,
            r.c.failure_cause,
            lost_on.c.id.label("steady_wait_worker_id"),
            lost_on.c.name.label("steady_wait_worker"),
            case((beating(lost_on), lost_on.c.steady_since + STEADY), else_=None).label("steady_wait_at"),
        )
        .join_from(r, p, p.c.id == r.c.project_id)
        .join(u, u.c.id == r.c.dispatched_by)
        .outerjoin(w, w.c.id == r.c.worker_id)
        .outerjoin(parent, and_(r.c.state == "queued", parent.c.id == r.c.parent_run_id, parent.c.state == "lost"))
        .outerjoin(lost_on, and_(lost_on.c.id == parent.c.worker_id, ~func.coalesce(steady(lost_on), False)))
    )


def _run_of(row) -> Run:
    values = dict(row._mapping)
    worker_id, worker, steady_at = (values.pop(f"steady_wait_{key}") for key in ("worker_id", "worker", "at"))
    wait = None if worker_id is None else SteadyWait(worker_id=worker_id, worker=worker, steady_at=steady_at)
    return Run(**values, steady_wait=wait)


def runs_of(rows) -> list[Run]:
    return [_run_of(row) for row in rows]


async def run_views(conn: AsyncConnection, run_ids: list[int]) -> list[Run]:
    r = tables.runs
    return runs_of(await conn.execute(run_select().where(r.c.id.in_(list(run_ids))).order_by(r.c.id)))


async def run_view(conn: AsyncConnection, run_id: int) -> Run:
    (found,) = await run_views(conn, [run_id])
    return found


@dataclass
class Activity:
    """The active runs of a plan: those of its steps by step key, and its plan run."""

    steps: dict[str, ActiveRun]
    plan_run: ActiveRun | None


async def plan_activity(conn: AsyncConnection, project_id: int, plan_id: str) -> Activity:
    r, u = tables.runs, tables.users
    query = (
        select(r.c.kind, r.c.step_key, r.c.id, r.c.state, u.c.login)
        .join_from(r, u, u.c.id == r.c.dispatched_by)
        .where(
            r.c.project_id == project_id,
            r.c.plan_id == plan_id,
            r.c.state.in_(runs.ACTIVE_STATES),
            r.c.kind != "author",  # an author run writes what the plan says, never a step's progress
        )
        .order_by(r.c.id)
    )
    activity = Activity(steps={}, plan_run=None)
    for row in await conn.execute(query):
        active = ActiveRun(id=row.id, state=row.state, dispatched_by=row.login)
        if row.kind == "plan":
            activity.plan_run = active
        else:
            activity.steps[row.step_key] = active
    return activity


def plan_lock_key(project_id: int, plan_id: str) -> str:
    """The text whose hash names the advisory lock a dispatch of a plan's steps or of its plan run takes."""
    return f"evo-runs:{project_id}:{plan_id}"


async def lock_plan(conn: AsyncConnection, project_id: int, plan_id: str) -> None:
    """Take the plan's dispatch lock until the caller's transaction ends: a dispatch of its steps and one of its plan
    run then each see the other's run, never both none."""
    key = func.hashtextextended(plan_lock_key(project_id, plan_id), 0)
    await conn.execute(select(func.pg_advisory_xact_lock(key)))


def text_or_none(value) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def busy(active: ActiveRun) -> str:
    return f"it has run #{active.id}, {active.state}, dispatched by {active.dispatched_by}"


def plan_busy(plan_id: str, active: ActiveRun) -> str:
    return f"plan {plan_id} has plan run #{active.id}, {active.state}, dispatched by {active.dispatched_by}"


async def step_readiness(engine, user: Principal, project: str, plan_id: str, sink: str | None) -> ReadySteps:
    """Every step of the plan with whether it may be dispatched now, and why not."""
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        plan_routes._reader(access)
        held = await plan_routes._visible(conn, access, plan_id, sink)
        activity = await plan_activity(conn, access.project_id, plan_id)
    body = held.body
    steps = body.get("steps") if isinstance(body.get("steps"), list) else []
    shown = []
    for index, step in enumerate(steps):
        key = step_key(step, index)
        reason = runs.unready_reason(body, step)
        running = activity.steps.get(key)
        if reason is None and running is not None:
            reason = busy(running)
        if reason is None and activity.plan_run is not None:
            reason = plan_busy(plan_id, activity.plan_run)
        mapping = step if isinstance(step, dict) else {}
        repo = runs.plan_repo(body, mapping) if isinstance(step, dict) else None
        shown.append(
            StepReadiness(
                key=key,
                title=text_or_none(mapping.get("title")),
                repo=text_or_none(repo.get("repo")) if repo else None,
                status=text_or_none(mapping.get("status", "pending")) if isinstance(step, dict) else None,
                ready=reason is None,
                reason=reason,
                active_run=running,
            )
        )
    return ReadySteps(project=project, plan_id=plan_id, revision=held.revision, plan_run=activity.plan_run, steps=shown)


_PLANS_OF = select(tables.plans.c.plan_id, tables.plans.c.label).where(
    tables.plans.c.project_id == bindparam("project_id")
)  # built once: every read of runs or decisions runs it


async def visible_plans(conn: AsyncConnection, access: ProjectAccess, sink: str | None) -> list[str]:
    """The plans of the project whose runs the caller may read: those it may read through ``sink`` (the project's
    hub sink when None), by the plan's label. A run of a plan no longer on the hub is shown to nobody."""
    plan_routes._reader(access)
    through = plan_routes._sink(access, sink)
    rows = await conn.execute(_PLANS_OF, {"project_id": access.project_id})
    return [row.plan_id for row in rows if access.visible(row.label, through)]


async def readable_run(
    conn: AsyncConnection, user: Principal, project: str, run_id: int, sink: str | None
) -> ProjectAccess:
    """The caller's access to ``project`` when it may read run ``run_id`` of it: a grant on the project (404 without,
    403 for a hub admin without one) and the run's plan visible to it (404 otherwise, as for no run). A review run,
    which has no plan, reads as what it reads: the project, through the project's default label; so does an author run
    of a new plan."""
    access = await project_access(conn, user, project)
    plan_routes._reader(access)
    r = tables.runs
    found = select(r.c.plan_id, r.c.kind).where(r.c.id == run_id, r.c.project_id == access.project_id)
    row = (await conn.execute(found)).one_or_none()
    if row is not None and row.plan_id is None:  # a review run, or an author run of a new plan
        visible = sees_unplanned(access, sink)
    else:
        visible = row is not None and row.plan_id in await visible_plans(conn, access, sink)
    if not visible:
        raise HTTPException(404, f"project {project} has no run {run_id}: see GET /v1/projects/{project}/runs")
    return access


def sees_unplanned(access: ProjectAccess, sink: str | None) -> bool:
    """Whether the caller reads the runs of the project on no plan: through ``sink``, the project's default label."""
    return access.visible(access.rules.default_label, plan_routes._sink(access, sink))


async def current_plan(conn: AsyncConnection, project_id: int, plan_id: str):
    """(body, revision) of the plan as the hub holds it now; None when it is not on the hub."""
    plans = tables.plans
    query = select(plans.c.body, plans.c.revision).where(plans.c.project_id == project_id, plans.c.plan_id == plan_id)
    return (await conn.execute(query)).one_or_none()
