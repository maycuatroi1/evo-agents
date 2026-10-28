"""The routes of a project's daily release: its schedule, pausing it, checking now, and where it stands
(``evo_agents.hub.releases`` describes the model, ``evo_agents.hub.server.release_check`` the check and its job).

GET /v1/projects/{p}/releases (reader) says where the release stands: the schedule, or null before an admin sets one;
whether it is paused; the moment of its next check; the latest checks, newest first, with the last one apart; and the
cycle of the schedule that is open or stopped, with its runs and their cost, for each run the version and branch it
pushed and how the hub landed it (its pull request, merge commit and tag, or why not), the tags it follows with the
state of their CI, the workflow run, the tests that failed and what kept the hub from reading it
(``actions_permission`` when the App's installation grants no actions), and for a stopped cycle why, the decision it
asks its owner and its thread in the triage chat.

PUT .../releases/schedule (admin of the project) sets the schedule: a key left out keeps what the schedule holds, or
takes its default (``releases.DEFAULT_*``) when there is none yet. The repo is one of the project's, on GitHub; with
no repo named and none set, the project's only repo on GitHub. The time zone must be one Postgres knows. The admin who
sets it becomes its owner, whom its runs are dispatched as. Audited as release.set.

POST .../releases/pause (admin) pauses the schedule: no check reads anything or opens a cycle until it is resumed,
the release runs of the project still queued are cancelled, and an open cycle left with no active run closes; a second
pause keeps the first one's time. POST .../releases/resume (admin) lets it check again. Neither touches the night
shift, nor does the night shift's pause, resume or circuit breaker touch them. Audited as release.pause and
release.resume.

POST .../releases/run (admin) makes a check now, through the same gates as the job's, and answers it; audited as
release.run. A member without the admin role gets 403 on each write, and a worker token 403 from the guard of /v1.

GET .../releases/cycles (reader) lists the cycles of the project that ended, published or closed, newest first, a page
of ``limit`` from ``offset``, each as the status shows the cycle open now: its runs, its tags and their CI, its cost
and why it ended. The web's Releases page lists them under the cycle open now.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Text, case, cast, column, exists, func, insert, literal, select, table, update
from sqlalchemy.dialects.postgresql import INTERVAL
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, release_ci, release_runs, releases, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.credentials import github_repo
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.night_cost import usage_cost
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.release_check import cancel_queued, check, close_idle, schedules, unfinished
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["releases"], responses={401: {"model": ErrorBody}})

SET = "release.set"  # an admin set the schedule: "<project> repo=<repo> at=<HH:MM> <time zone>"
PAUSE = "release.pause"  # "<project>", the queued runs it cancelled and the cycle it closed in the log line
RESUME = "release.resume"
RUN = "release.run"  # an admin checked now: "<project> check:<id>"
PG_TIMEZONES = table("pg_timezone_names", column("name", Text))
WRITES = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 409: {"model": ErrorBody}}
READS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}
ENDED = tuple(state for state in releases.CYCLE_STATES if state not in releases.UNFINISHED)  # published, closed
SHOWN_CYCLES = 20  # the ended cycles one page of GET .../releases/cycles lists by default
MAX_CYCLES = 100
MAX_CYCLES_OFFSET = 10_000


# Models


class ReleaseScheduleWrite(BaseModel):
    """What PUT .../releases/schedule sets; a key left out keeps what the schedule holds, or its default."""

    model_config = ConfigDict(extra="forbid")

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
    trigger: Literal[releases.CHECK_TRIGGERS] = Field(description="schedule, the daily job; manual, an admin's run")
    local_day: date = Field(description="the day of the check in the schedule's time zone")
    checked_at: datetime
    requested_by: str | None = Field(description="the admin of a manual check")
    outcome: Literal[releases.CHECK_OUTCOMES] = Field(
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
    mode: Literal[releases.RUN_MODES]
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
    land_state: Literal[release_runs.LAND_STATES] | None = Field(
        None, description="tagged; refused, with the reason; failed, GitHub failing; null while it waits to land"
    )
    land_reason: str | None = None


class ReleaseTag(BaseModel):
    tag: str
    commit: str
    run_id: int | None = Field(description="the run whose merge commit the hub tagged; null for a tag a person made")
    ci_state: Literal[release_ci.CI_STATES] = Field(description="the CI of the schedule's workflow for the tag")
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
    state: Literal[releases.CYCLE_STATES]
    start: Literal[releases.RUN_MODES] = Field(description="the mode of its first run")
    owner: str
    local_day: date
    base_tag: str | None
    base_commit: str | None
    target_ref: str
    target_commit: str | None
    changed_paths: list[str]
    cost_usd: float = Field(description="what they cost so far, each agent session once")
    stop_reason: str | None
    stop_cause: Literal[release_ci.STOP_CAUSES] | None = Field(description="why it stopped last")
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


# Reading


def _admin(access: ProjectAccess, doing: str) -> None:
    if not has_role(access.role, "admin"):
        held = access.role or "no grant"
        raise HTTPException(403, f"{doing} of project {access.name} needs the admin role on it; you hold {held}")


def _reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise HTTPException(403, f"reading the release of project {access.name} needs a grant on it")


def _next_check(row):
    """The next moment, in UTC, whose local time in the schedule's zone is its check_at."""
    local_now = func.timezone(row.timezone, func.now())
    today = func.date_trunc("day", local_now) + cast(literal(row.check_at), INTERVAL)
    upcoming = case((today > local_now, today), else_=today + timedelta(days=1))
    return func.timezone(row.timezone, upcoming)


async def _origin(conn: AsyncConnection, project_id: int, repo: str) -> str | None:
    r = tables.project_repos
    query = select(r.c.origin).where(r.c.project_id == project_id, r.c.name == repo)
    return (await conn.execute(query)).scalar_one_or_none()


async def _schedule_view(conn: AsyncConnection, row) -> ReleaseSchedule:
    u = tables.users
    writer = (await conn.execute(select(u.c.login).where(u.c.id == row.updated_by))).scalar_one()
    found = github_repo(await _origin(conn, row.project_id, row.repo) or "")
    return ReleaseSchedule(
        repo=row.repo,
        github_repo="/".join(found) if found else None,
        at=row.check_at,
        timezone=row.timezone,
        target_ref=row.target_ref,
        tag_pattern=row.tag_pattern,
        workflow=row.workflow,
        paths=list(row.paths),
        owner=row.owner,
        updated_by=writer,
        created_at=row.created_at,
        updated_at=row.updated_at,
        paused_at=row.paused_at,
        paused_by=row.paused_by_login,
    )


def _checks(schedule_id: int, limit: int):
    k, u = tables.release_checks, tables.users
    return (
        select(k, u.c.login.label("requested_login"))
        .outerjoin(u, u.c.id == k.c.requested_by)
        .where(k.c.schedule_id == schedule_id)
        .order_by(k.c.id.desc())
        .limit(limit)
    )


def check_view(row) -> ReleaseCheck:
    return ReleaseCheck(
        id=row.id,
        trigger=row.trigger,
        local_day=row.local_day,
        checked_at=row.checked_at,
        requested_by=row.requested_login,
        outcome=row.outcome,
        reason=row.reason,
        base_tag=row.base_tag,
        base_commit=row.base_commit,
        target_commit=row.target_commit,
        changed_paths=None if row.changed_paths is None else list(row.changed_paths),
        cycle_id=row.cycle_id,
        run_id=row.run_id,
    )


def _cycles():
    """The cycles with their owner's login, for ``_cycle_views``."""
    c, u = tables.release_cycles, tables.users
    return select(c, u.c.login.label("owner")).join_from(c, u, u.c.id == c.c.owner_id)


def _runs_of(cycle_ids: list[int]):
    """The runs of the cycles ``cycle_ids`` with their state, cost and landing, by cycle then run."""
    rr, r = tables.release_runs, tables.runs
    landed = (rr.c.version, rr.c.branch, rr.c.decision, rr.c.pull_url, rr.c.merge_commit, rr.c.tagged, rr.c.tagged_at)
    return (
        select(
            rr.c.cycle_id,
            rr.c.run_id,
            rr.c.mode,
            rr.c.tag,
            r.c.state,
            usage_cost(r.c.usage).label("cost"),
            *landed,
            rr.c.land_state,
            rr.c.land_reason,
        )
        .join_from(rr, r, r.c.id == rr.c.run_id)
        .where(rr.c.cycle_id.in_(cycle_ids))
        .order_by(rr.c.cycle_id, rr.c.run_id)
    )


def _run_view(row) -> ReleaseCycleRun:
    values = {key: value for key, value in row._mapping.items() if key not in ("cycle_id", "cost")}
    return ReleaseCycleRun(**values, cost_usd=float(row.cost))


def _cycle_model(found, items: list[ReleaseCycleRun], tags: list[ReleaseTag]) -> ReleaseCycle:
    thread = chats.chat_link(found.triage_chat_id, found.triage_root_id) if found.triage_root_id else None
    return ReleaseCycle(
        id=found.id,
        state=found.state,
        start=found.start,
        owner=found.owner,
        local_day=found.local_day,
        base_tag=found.base_tag,
        base_commit=found.base_commit,
        target_ref=found.target_ref,
        target_commit=found.target_commit,
        changed_paths=list(found.changed_paths),
        cost_usd=round(sum(item.cost_usd for item in items), 4),
        stop_reason=found.stop_reason,
        stop_cause=found.stop_cause,
        decision_id=found.decision_id,
        thread=thread,
        opened_at=found.opened_at,
        ended_at=found.ended_at,
        runs=items,
        tags=tags,
    )


async def _cycle_views(conn: AsyncConnection, found: list) -> list[ReleaseCycle]:
    """The cycle rows ``found`` (of ``_cycles``) as the API shows them, in their order, with their runs and tags read
    in one query each whatever their number."""
    ids = [row.id for row in found]
    if not ids:
        return []
    runs: dict[int, list[ReleaseCycleRun]] = {cycle_id: [] for cycle_id in ids}
    for row in (await conn.execute(_runs_of(ids))).all():
        runs[row.cycle_id].append(_run_view(row))
    tags: dict[int, list[ReleaseTag]] = {cycle_id: [] for cycle_id in ids}
    t = tables.release_tags
    for row in (await conn.execute(select(t).where(t.c.cycle_id.in_(ids)).order_by(t.c.id))).all():
        tags[row.cycle_id].append(_tag_view(row))
    return [_cycle_model(row, runs[row.id], tags[row.id]) for row in found]


async def _cycle_view(conn: AsyncConnection, cycle_id: int) -> ReleaseCycle:
    found = (await conn.execute(_cycles().where(tables.release_cycles.c.id == cycle_id))).one()
    return (await _cycle_views(conn, [found]))[0]


def _tag_view(row) -> ReleaseTag:
    failures = row.failures or {}
    return ReleaseTag(
        tag=row.tag,
        commit=row.commit,
        run_id=row.run_id,
        ci_state=row.ci_state,
        workflow_url=row.workflow_url,
        run_attempt=row.run_attempt,
        reruns=row.reruns,
        failed_tests=list(failures.get("ids") or []),
        failed_jobs=[str(job.get("job")) for job in failures.get("jobs") or [] if isinstance(job, dict)],
        infra=failures.get("infra") is True,
        problem=row.problem,
        watched_since=row.watched_since,
        checked_at=row.checked_at,
    )


async def _schedule_row(conn: AsyncConnection, project_id: int, *, lock: bool = False):
    query = schedules().where(tables.release_schedules.c.project_id == project_id)
    if lock:
        query = query.with_for_update(of=tables.release_schedules)
    return (await conn.execute(query)).one_or_none()


async def release_status(conn: AsyncConnection, access: ProjectAccess) -> ReleaseStatus:
    row = await _schedule_row(conn, access.project_id)
    if row is None:
        return ReleaseStatus(
            project=access.name, schedule=None, paused=False, next_check_at=None, last_check=None, checks=[], cycle=None
        )
    shown = [check_view(item) for item in (await conn.execute(_checks(row.id, releases.SHOWN_CHECKS))).all()]
    cycle = await unfinished(conn, row.id)
    return ReleaseStatus(
        project=access.name,
        schedule=await _schedule_view(conn, row),
        paused=row.paused_at is not None,
        next_check_at=(await conn.execute(select(_next_check(row)))).scalar_one(),
        last_check=shown[0] if shown else None,
        checks=shown,
        cycle=None if cycle is None else await _cycle_view(conn, cycle.id),
    )


@router.get("/{project}/releases", response_model=ReleaseStatus, responses=READS)
async def status(request: Request, project: ProjectName, user: CurrentUser) -> ReleaseStatus:
    """Where the project's daily release stands: its schedule, its latest checks and the cycle open now."""
    async with request.app.state.engine.connect() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        return await release_status(conn, access)


@router.get("/{project}/releases/cycles", response_model=ReleaseCycleList, responses=READS)
async def ended_cycles(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=MAX_CYCLES, description="the cycles of one page")] = SHOWN_CYCLES,
    offset: Annotated[int, Query(ge=0, le=MAX_CYCLES_OFFSET, description="the cycles before the page")] = 0,
) -> ReleaseCycleList:
    """The project's release cycles that ended, published or closed, newest first, each with its runs and tags."""
    c = tables.release_cycles
    async with request.app.state.engine.connect() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        ended = (c.c.project_id == access.project_id, c.c.state.in_(ENDED))
        total = (await conn.execute(select(func.count()).select_from(c).where(*ended))).scalar_one()
        page = _cycles().where(*ended).order_by(c.c.id.desc()).limit(limit).offset(offset)
        found = (await conn.execute(page)).all()
        cycles = await _cycle_views(conn, found)
    return ReleaseCycleList(project=access.name, cycles=cycles, total=total, limit=limit, offset=offset)


# Writing


async def _github_repos(conn: AsyncConnection, project_id: int) -> dict[str, str]:
    """{name: owner/name} of the project's repos on GitHub."""
    r = tables.project_repos
    rows = (await conn.execute(select(r.c.name, r.c.origin).where(r.c.project_id == project_id))).all()
    return {row.name: "/".join(found) for row in rows if row.origin and (found := github_repo(row.origin))}


async def _repo(conn: AsyncConnection, access: ProjectAccess, asked: str | None, current) -> str:
    on_github = await _github_repos(conn, access.project_id)
    name = asked or (current.repo if current is not None else None)
    if name is None:
        if len(on_github) != 1:
            listed = ", ".join(sorted(on_github)) or "none"
            raise HTTPException(422, f"name the repo of the release: the repos of {access.name} on GitHub are {listed}")
        name = next(iter(on_github))
    if name not in on_github:
        raise HTTPException(
            422, f"{name} is not a repo of {access.name} with an origin on GitHub: a release is cut and tagged there"
        )
    return name


async def _time_zone(conn: AsyncConnection, name: str) -> str:
    if not (await conn.execute(select(exists().where(PG_TIMEZONES.c.name == name)))).scalar_one():
        raise HTTPException(422, f"{name!r} is not a time zone the hub knows: give an IANA name such as Asia/Tokyo")
    return name


async def _values(conn: AsyncConnection, access: ProjectAccess, body: ReleaseScheduleWrite, current) -> dict:
    """The schedule's columns once ``body`` is set over ``current`` (None: the defaults)."""

    def kept(asked, column_name: str, default):
        if asked is not None:
            return asked
        return getattr(current, column_name) if current is not None else default

    values = {
        "repo": await _repo(conn, access, body.repo, current),
        "check_at": kept(body.at, "check_at", releases.DEFAULT_AT),
        "timezone": kept(body.timezone, "timezone", releases.DEFAULT_TIMEZONE),
        "target_ref": kept(body.target_ref, "target_ref", releases.DEFAULT_TARGET_REF),
        "tag_pattern": kept(body.tag_pattern, "tag_pattern", releases.DEFAULT_TAG_PATTERN),
        "workflow": kept(body.workflow, "workflow", releases.DEFAULT_WORKFLOW),
        "paths": list(kept(body.paths, "paths", list(releases.DEFAULT_PATHS))),
    }
    await _time_zone(conn, values["timezone"])
    return values


@router.put(
    "/{project}/releases/schedule", response_model=ReleaseStatus, responses={**WRITES, 422: {"model": ErrorBody}}
)
async def set_schedule(
    request: Request, project: ProjectName, body: ReleaseScheduleWrite, user: CurrentUser
) -> ReleaseStatus:
    """Set the project's release schedule; the caller becomes its owner."""
    s = tables.release_schedules
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _admin(access, "setting the release schedule")
        current = await _schedule_row(conn, access.project_id, lock=True)
        values = await _values(conn, access, body, current) | {"owner_id": user.user_id, "updated_by": user.user_id}
        if current is None:
            await conn.execute(insert(s).values(project_id=access.project_id, **values))
        else:
            await conn.execute(update(s).values(**values, updated_at=func.now()).where(s.c.id == current.id))
        target = f"{access.name} repo={values['repo']} at={values['check_at']} {values['timezone']}"
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=SET, target=target, project_id=access.project_id
        )
        found = await release_status(conn, access)
    log.info("release schedule set", extra={"project": project, "login": user.login, "repo": values["repo"]})
    return found


async def _admin_schedule(conn: AsyncConnection, user: Principal, project: str, doing: str):
    access = await project_access(conn, user, project)
    _admin(access, f"{doing} the release")
    row = await _schedule_row(conn, access.project_id, lock=True)
    if row is None:
        raise HTTPException(
            409, f"project {access.name} has no release schedule: an admin sets one with `evo-agents hub release set`"
        )
    return access, row


@router.post("/{project}/releases/pause", response_model=ReleaseStatus, responses=WRITES)
async def pause(request: Request, project: ProjectName, user: CurrentUser) -> ReleaseStatus:
    """Pause the release schedule, cancel the release runs still queued and close an open cycle left idle."""
    s = tables.release_schedules
    async with request.app.state.engine.begin() as conn:
        access, row = await _admin_schedule(conn, user, project, "pausing")
        await conn.execute(
            update(s)
            .values(paused_at=func.now(), paused_by=user.user_id, updated_at=func.now())
            .where(s.c.id == row.id, s.c.paused_at.is_(None))
        )
        reason = f"the release schedule of {access.name} was paused by {user.login}"
        cancelled = await cancel_queued(conn, access.project_id, reason)
        closed = await close_idle(conn, row.id, reason)
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=PAUSE,
            target=access.name,
            project_id=access.project_id,
        )
        found = await release_status(conn, access)
    log.info(
        "release paused", extra={"project": project, "login": user.login, "cancelled": cancelled, "closed": closed}
    )
    return found


@router.post("/{project}/releases/resume", response_model=ReleaseStatus, responses=WRITES)
async def resume(request: Request, project: ProjectName, user: CurrentUser) -> ReleaseStatus:
    """Let the release schedule check again."""
    s = tables.release_schedules
    async with request.app.state.engine.begin() as conn:
        access, row = await _admin_schedule(conn, user, project, "resuming")
        await conn.execute(
            update(s).values(paused_at=None, paused_by=None, updated_at=func.now()).where(s.c.id == row.id)
        )
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=RESUME,
            target=access.name,
            project_id=access.project_id,
        )
        found = await release_status(conn, access)
    log.info("release resumed", extra={"project": project, "login": user.login})
    return found


@router.post("/{project}/releases/run", response_model=ReleaseCheck, responses=WRITES)
async def run_now(request: Request, project: ProjectName, user: CurrentUser) -> ReleaseCheck:
    """Check now, through the gates of the daily check, and answer the check."""
    engine = request.app.state.engine
    async with engine.begin() as conn:
        access, row = await _admin_schedule(conn, user, project, "checking")
    found = await check(engine, request.app.state.github_app, row.id, trigger="manual", requested_by=user.user_id)
    if found is None:  # the schedule went in between: nothing was checked
        raise HTTPException(409, f"project {access.name} has no release schedule any more: nothing was checked")
    async with engine.begin() as conn:
        target = f"{access.name} check:{found.id}"
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=RUN, target=target, project_id=access.project_id
        )
        k = tables.release_checks
        shown = (await conn.execute(_checks(row.id, 1).where(k.c.id == found.id))).one()
    log.info("release checked", extra={"project": project, "login": user.login, "outcome": found.outcome})
    return check_view(shown)
