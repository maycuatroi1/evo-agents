"""The cron jobs of a project (``docs/hub.md``, Cron jobs; plan hub-cron-jobs, criterion a1).

- GET /v1/projects/{p}/cron-jobs: its jobs, by name, each with its next fire in its time zone, its last fire and what
  the runs of its fires cost in the last 24 hours.
- GET .../cron-jobs/{name}: one.
- PUT .../cron-jobs/{name} {cron, timezone, kind, ..., if_revision}: creates it (no ``if_revision``, or 0), owned by
  the caller, or stores what it starts as its next revision (``if_revision`` its revision now; 409
  ``revision_conflict`` otherwise, with the job as the hub holds it). 422 with the reason for a cron that does not
  read, a time zone the hub does not know, a job of kind agent or plan that fires more often than every 15 minutes, a
  job of kind command more often than every 5 (``evo_agents.hub.cron``), or work that names what the project lacks.
- POST .../cron-jobs/{name}/pause {reason}, .../resume: a paused job does not fire.
- POST .../cron-jobs/{name}/run: fire it now, a fire due at the moment it was pressed, which the hub handles as any
  fire (outcome null until then).
- GET .../cron-jobs/{name}/fires[?limit=N]: its fires, the latest due first.
- DELETE .../cron-jobs/{name}: deletes it with its fires; its runs stay.

Every member of the project reads its jobs and their fires. A writer makes jobs, and changes, pauses, resumes, fires and
deletes their own; an admin of the project pauses, resumes, fires and deletes anyone's, but changes only their own:
what a job starts runs as its owner. Writes are audited as ``cron.*``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select

from evo_agents.hub import cron, cron_store, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.night_cost import usage_cost
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.tables_cron import KINDS, NAME_PATTERN

log = logging.getLogger(__name__)

REVISION_CONFLICT = "revision_conflict"
DEFAULT_TIMEOUT = 30  # minutes a command may run
MAX_TIMEOUT = 240
MAX_PROMPT = 20_000
MAX_COMMAND = 4_000
COST_WINDOW = timedelta(hours=24)
router = APIRouter(prefix="/v1/projects", tags=["cron-jobs"], responses={401: {"model": ErrorBody}})
Name = Annotated[str, Path(min_length=1, max_length=63, pattern=NAME_PATTERN, description="the job's name")]
Kind = Literal["agent", "plan", "command"]
assert set(KINDS) == {"agent", "plan", "command"}


class CronFireOut(BaseModel):
    id: int
    scheduled_for: datetime = Field(description="the minute it was due; for run now, the moment it was pressed")
    manual: bool = Field(description="someone pressed run now")
    actor: str | None = Field(description="who pressed run now; null for a fire of the schedule")
    outcome: Literal["started", "busy", "missed", "refused"] | None = Field(
        description="started a run; busy: the run of a fire before was still queued or running; missed: the hub was "
        "down at its minute; refused, with the reason; null until the hub handled it"
    )
    reason: str | None
    run_id: int | None = Field(description="the run it started")
    created_at: datetime
    handled_at: datetime | None


class CronJobOut(BaseModel):
    project: str
    name: str
    cron: str = Field(description="five fields: minute, hour, day of month, month, day of week")
    timezone: str = Field(description="the IANA time zone its cron reads in")
    kind: Kind = Field(description="agent: a message to an agent in a new thread; plan: a plan run; command: a shell")
    agent: str | None = Field(description="kind agent: the handle of the agent it messages")
    chat_id: int | None = Field(description="kind agent: the chat of its threads; null: the owner's direct chat")
    prompt: str | None = Field(description="kind agent: the message")
    plan: str | None = Field(description="kind plan: the plan it runs")
    command: str | None = Field(description="kind command: the shell command line")
    repo: str | None = Field(description="kind command: the repo it runs in; null: an empty directory")
    timeout_minutes: int | None = Field(description="kind command: how long it may run")
    owner: str = Field(description="the login of who made it: its runs run as them")
    revision: int
    paused: bool
    paused_at: datetime | None
    paused_by: str | None = Field(description="who paused it; null when the hub did, or it runs")
    paused_reason: str | None
    next_fire_at: datetime | None = Field(description="its next minute, in UTC; null while paused")
    last_fire: CronFireOut | None
    cost_24h_usd: float = Field(description="what the runs of its fires cost in the last 24 hours")
    can_change: bool = Field(description="the caller may change what it starts: its owner, holding writer")
    can_manage: bool = Field(description="the caller may pause, resume, fire and delete it: its owner, or an admin")
    created_at: datetime
    updated_at: datetime
    updated_by: str | None


class CronJobWritten(CronJobOut):
    created: bool
    changed: bool = Field(description="false when the hub held exactly this: no revision was added")


class CronJobPut(BaseModel):
    cron: str = Field(description="five fields: minute, hour, day of month, month, day of week")
    timezone: str = Field("UTC", description="an IANA time zone, such as Asia/Ho_Chi_Minh")
    kind: Kind
    agent: str | None = Field(None, description="kind agent: the handle of an agent of the project")
    chat_id: int | None = Field(None, description="kind agent: a chat of yours; none: your direct chat with the agent")
    prompt: str | None = Field(None, description="kind agent: the message")
    plan: str | None = Field(None, description="kind plan: a plan of the project")
    command: str | None = Field(None, description="kind command: the shell command line")
    repo: str | None = Field(None, description="kind command: a repo of the project; none: an empty directory")
    timeout_minutes: int | None = Field(None, description="kind command: 1 to 240 minutes (30)")
    if_revision: int | None = Field(
        None, ge=0, description="the revision it is at now; none or 0 creates it, and is 409 when it exists"
    )


class CronJobPause(BaseModel):
    reason: str | None = Field(None, max_length=500, description="why, shown with the pause")


class CronJobDeleted(BaseModel):
    project: str
    deleted: str


class CronJobConflict(ErrorBody):
    current: CronJobOut | None = Field(None, description="the job as the hub holds it")


WRITE_REFUSALS: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    409: {"model": CronJobConflict},
    422: {"model": ErrorBody},
}
REFUSALS: dict[int | str, dict[str, Any]] = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}


class _Conflict(Exception):
    def __init__(self, message: str, current: CronJobOut):
        super().__init__(message)
        self.message, self.current = message, current


def _conflict(request: Request, exc: _Conflict) -> JSONResponse:
    body = CronJobConflict(
        error=REVISION_CONFLICT,
        message=exc.message,
        request_id=getattr(request.state, "request_id", None),
        current=exc.current,
    )
    return JSONResponse(body.model_dump(mode="json", exclude_none=True), status_code=409)


# Who may


def _reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise HTTPException(403, f"reading the cron jobs of project {access.name} needs a grant on it")


def _writer(access: ProjectAccess, doing: str) -> None:
    if not has_role(access.role, "writer"):
        held = access.role or "no grant"
        raise HTTPException(
            403, f"{doing} a cron job of project {access.name} needs the writer role on it; you hold {held}"
        )


def can_change(access: ProjectAccess, user: Principal, job: cron_store.Job) -> bool:
    return has_role(access.role, "writer") and job.owner_id == user.user_id


def can_manage(access: ProjectAccess, user: Principal, job: cron_store.Job) -> bool:
    return can_change(access, user, job) or has_role(access.role, "admin")


def _manager(access: ProjectAccess, user: Principal, job: cron_store.Job, doing: str) -> None:
    if not can_manage(access, user, job):
        raise HTTPException(
            403,
            f"{doing} cron job {job.name} of project {access.name} needs its owner ({job.owner}) with the writer role, "
            "or the admin role on the project",
        )


def _not_found(access: ProjectAccess, name: str) -> HTTPException:
    return HTTPException(
        404, f"project {access.name} has no cron job {name}: see `evo-agents hub cron list --project {access.name}`"
    )


# Out


def _fire(row: dict | None) -> CronFireOut | None:
    return None if row is None else CronFireOut(**{k: v for k, v in row.items() if k != "job_id"})


def _next(job: cron_store.Job) -> datetime | None:
    if job.paused_at is not None:
        return None
    try:
        return cron.next_fire(job.cron, job.timezone)
    except Exception:  # a zone this hub lost, or a cron a newer croniter reads otherwise: shown, never fired
        log.warning("cron job has no next fire", extra={"project": job.project, "cron_job": job.name})
        return None


def _out(access, user, job: cron_store.Job, last: dict | None, cost: float) -> CronJobOut:
    return CronJobOut(
        project=job.project,
        name=job.name,
        cron=job.cron,
        timezone=job.timezone,
        kind=job.kind,  # type: ignore[arg-type]  # the table's check holds it to KINDS
        agent=job.agent,
        chat_id=job.chat_id,
        prompt=job.prompt,
        plan=job.plan_id,
        command=job.command,
        repo=job.repo,
        timeout_minutes=job.timeout_minutes,
        owner=job.owner,
        revision=job.revision,
        paused=job.paused_at is not None,
        paused_at=job.paused_at,
        paused_by=job.paused_by,
        paused_reason=job.paused_reason,
        next_fire_at=_next(job),
        last_fire=_fire(last),
        cost_24h_usd=round(cost, 4),
        can_change=can_change(access, user, job),
        can_manage=can_manage(access, user, job),
        created_at=job.created_at,
        updated_at=job.updated_at,
        updated_by=job.updated_by,
    )


async def _costs(conn, job_ids: list[int]) -> dict[int, float]:
    """job id -> what the runs of its fires cost in the last COST_WINDOW."""
    if not job_ids:
        return {}
    r = tables.runs
    since = datetime.now(UTC) - COST_WINDOW
    query = (
        select(r.c.cron_job_id, func.coalesce(func.sum(usage_cost(r.c.usage)), 0).label("cost"))
        .where(and_(r.c.cron_job_id.in_(job_ids), r.c.queued_at >= since))
        .group_by(r.c.cron_job_id)
    )
    return {row.cron_job_id: float(row.cost or 0) for row in await conn.execute(query)}


async def _shown(conn, access, user, job: cron_store.Job) -> CronJobOut:
    last = await cron_store.last_fires(conn, [job.id])
    cost = await _costs(conn, [job.id])
    return _out(access, user, job, last.get(job.id), cost.get(job.id, 0.0))


async def _record(conn, user: Principal, access: ProjectAccess, action: str, job_name: str, revision: int) -> None:
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=action,
        target=f"{access.name} cron={job_name} revision={revision}",
        project_id=access.project_id,
    )


# What a job starts


def _refuse(message: str) -> HTTPException:
    return HTTPException(422, message)


def _blank(value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _only(body: CronJobPut, allowed: tuple[str, ...]) -> None:
    given = [
        key
        for key in ("agent", "chat_id", "prompt", "plan", "command", "repo", "timeout_minutes")
        if getattr(body, key) is not None and key not in allowed
    ]
    if given:
        raise _refuse(f"a job of kind {body.kind} takes no {', '.join(given)}: those belong to another kind")


async def _agent_work(conn, access: ProjectAccess, user: Principal, body: CronJobPut) -> dict:
    _only(body, ("agent", "chat_id", "prompt"))
    handle, prompt = _blank(body.agent), _blank(body.prompt)
    if handle is None or prompt is None:
        raise _refuse("a job of kind agent needs agent, the handle of an agent of the project, and prompt")
    if len(prompt) > MAX_PROMPT:
        raise _refuse(f"the prompt is {len(prompt)} characters; a job takes at most {MAX_PROMPT}")
    ap = tables.agent_profiles
    found = (
        await conn.execute(
            select(ap.c.id).where(
                ap.c.project_id == access.project_id, ap.c.handle == handle.lstrip("@"), ap.c.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise _refuse(
            f"project {access.name} has no agent @{handle.lstrip('@')}: see `evo-agents hub agent list --project "
            f"{access.name}`"
        )
    if body.chat_id is not None:
        m = tables.chat_members
        member = (
            await conn.execute(
                select(m.c.chat_id).where(
                    m.c.chat_id == body.chat_id, m.c.user_id == user.user_id, m.c.removed_at.is_(None)
                )
            )
        ).first()
        if member is None:
            raise _refuse(f"you are not in chat {body.chat_id}: a job posts only in a chat of yours")
    return {"profile_id": found, "chat_id": body.chat_id, "prompt": prompt}


async def _plan_work(conn, access: ProjectAccess, body: CronJobPut) -> dict:
    _only(body, ("plan",))
    plan = _blank(body.plan)
    if plan is None:
        raise _refuse("a job of kind plan needs plan, a plan of the project")
    p = tables.plans
    held = (
        await conn.execute(select(p.c.plan_id).where(p.c.project_id == access.project_id, p.c.plan_id == plan))
    ).first()
    if held is None:
        raise _refuse(
            f"project {access.name} has no plan {plan}: see `evo-agents hub plan list --project {access.name}`"
        )
    return {"plan_id": plan}


async def _command_work(conn, access: ProjectAccess, body: CronJobPut) -> dict:
    _only(body, ("command", "repo", "timeout_minutes"))
    command = _blank(body.command)
    if command is None:
        raise _refuse("a job of kind command needs command, the shell command line it runs")
    if len(command) > MAX_COMMAND:
        raise _refuse(f"the command is {len(command)} characters; a job takes at most {MAX_COMMAND}")
    timeout = DEFAULT_TIMEOUT if body.timeout_minutes is None else body.timeout_minutes
    if not 1 <= timeout <= MAX_TIMEOUT:
        raise _refuse(f"timeout_minutes is {timeout}: a command runs 1 to {MAX_TIMEOUT} minutes")
    repo = _blank(body.repo)
    if repo is not None:
        pr = tables.project_repos
        held = (
            await conn.execute(select(pr.c.name).where(pr.c.project_id == access.project_id, pr.c.name == repo))
        ).first()
        if held is None:
            raise _refuse(f"project {access.name} has no repo {repo}: a command runs in a repo of the project or none")
    return {"command": command, "repo": repo, "timeout_minutes": timeout}


async def checked(conn, access: ProjectAccess, user: Principal, body: CronJobPut) -> dict:
    """The columns a write of ``body`` stores: cron, timezone, kind and every column of ``cron_store.WORK``; 422 with
    the reason when the hub does not take it."""
    try:
        expr = cron.check(body.cron, body.timezone, body.kind)
    except cron.CronRefused as exc:
        raise _refuse(str(exc)) from None
    if body.kind == "agent":
        work = await _agent_work(conn, access, user, body)
    elif body.kind == "plan":
        work = await _plan_work(conn, access, body)
    else:
        work = await _command_work(conn, access, body)
    return {"cron": expr, "timezone": body.timezone, "kind": body.kind, **dict.fromkeys(cron_store.WORK), **work}


def _same(job: cron_store.Job, fields: dict) -> bool:
    held = {"cron": job.cron, "timezone": job.timezone, "kind": job.kind, **job.work()}
    return held == fields


# Reading


@router.get("/{project}/cron-jobs", response_model=list[CronJobOut], responses=REFUSALS)
async def list_cron_jobs(request: Request, project: ProjectName, user: CurrentUser) -> list[CronJobOut]:
    """The cron jobs of the project, by name, with their next fire, last fire and cost in 24 hours."""
    async with request.app.state.engine.connect() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        jobs = await cron_store.listed(conn, access.project_id)
        ids = [job.id for job in jobs]
        last = await cron_store.last_fires(conn, ids)
        costs = await _costs(conn, ids)
    return [_out(access, user, job, last.get(job.id), costs.get(job.id, 0.0)) for job in jobs]


@router.get("/{project}/cron-jobs/{name}", response_model=CronJobOut, responses=REFUSALS)
async def show_cron_job(request: Request, project: ProjectName, name: Name, user: CurrentUser) -> CronJobOut:
    """One cron job."""
    async with request.app.state.engine.connect() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        job = await cron_store.one(conn, access.project_id, name)
        if job is None:
            raise _not_found(access, name)
        return await _shown(conn, access, user, job)


@router.get("/{project}/cron-jobs/{name}/fires", response_model=list[CronFireOut], responses=REFUSALS)
async def cron_job_fires(
    request: Request,
    project: ProjectName,
    name: Name,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=500, description="at most this many fires")] = 50,
) -> list[CronFireOut]:
    """The fires of a cron job, the latest due first."""
    async with request.app.state.engine.connect() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        job = await cron_store.one(conn, access.project_id, name)
        if job is None:
            raise _not_found(access, name)
        found = await cron_store.fires(conn, job.id, limit)
    return [fire for fire in map(_fire, found) if fire is not None]


# Changing


@router.put("/{project}/cron-jobs/{name}", response_model=CronJobWritten, responses=WRITE_REFUSALS)
async def put_cron_job(request: Request, project: ProjectName, name: Name, body: CronJobPut, user: CurrentUser):
    """Create the cron job, owned by the caller, or store what it starts as its next revision."""
    try:
        async with request.app.state.engine.begin() as conn:
            access = await project_access(conn, user, project)
            _writer(access, "changing")
            held = await cron_store.one(conn, access.project_id, name, lock=True)
            if held is None and body.if_revision:
                raise HTTPException(404, f"project {access.name} has no cron job {name} to replace")
            if held is not None and not can_change(access, user, held):
                raise HTTPException(
                    403,
                    f"cron job {name} is {held.owner}'s: what it starts runs as them, so only they change it; an admin "
                    "may pause, resume, fire or delete it",
                )
            if held is not None and body.if_revision != held.revision:
                if not body.if_revision:
                    message = f"cron job {name} exists at revision {held.revision}: pass if_revision {held.revision}"
                else:
                    message = (
                        f"cron job {name} is at revision {held.revision} on the hub, not {body.if_revision}: someone "
                        "changed it since you read it. Read it again and retry; nothing was written"
                    )
                raise _Conflict(message, await _shown(conn, access, user, held))
            fields = await checked(conn, access, user, body)
            if held is not None and _same(held, fields):
                out = await _shown(conn, access, user, held)
                return CronJobWritten(**out.model_dump(), created=False, changed=False)
            revision = await cron_store.write(conn, access.project_id, name, fields, user.user_id, held)
            action = audit.CRON_CREATE if held is None else audit.CRON_UPDATE
            await _record(conn, user, access, action, name, revision)
            written = await cron_store.one(conn, access.project_id, name)
            assert written is not None  # written in this transaction
            out = await _shown(conn, access, user, written)
    except _Conflict as exc:
        return _conflict(request, exc)
    log.info("cron job stored", extra={"project": project, "cron_job": name, "revision": revision})
    return CronJobWritten(**out.model_dump(), created=held is None, changed=True)


async def _managed(request: Request, project: str, name: str, user: Principal, doing: str, change) -> CronJobOut:
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        job = await cron_store.one(conn, access.project_id, name, lock=True)
        if job is None:
            _reader(access)
            raise _not_found(access, name)
        _manager(access, user, job, doing)
        action = await change(conn, job)
        await _record(conn, user, access, action, name, job.revision)
        held = await cron_store.one(conn, access.project_id, name)
        assert held is not None
        return await _shown(conn, access, user, held)


@router.post("/{project}/cron-jobs/{name}/pause", response_model=CronJobOut, responses=REFUSALS)
async def pause_cron_job(
    request: Request, project: ProjectName, name: Name, body: CronJobPause, user: CurrentUser
) -> CronJobOut:
    """Pause a cron job: it does not fire until resumed."""

    async def change(conn, job):
        await cron_store.pause(conn, job.id, user.user_id, _blank(body.reason))
        return audit.CRON_PAUSE

    return await _managed(request, project, name, user, "pausing", change)


@router.post("/{project}/cron-jobs/{name}/resume", response_model=CronJobOut, responses=REFUSALS)
async def resume_cron_job(request: Request, project: ProjectName, name: Name, user: CurrentUser) -> CronJobOut:
    """Resume a paused cron job: it fires from its next minute on."""

    async def change(conn, job):
        await cron_store.resume(conn, job.id)
        return audit.CRON_RESUME

    return await _managed(request, project, name, user, "resuming", change)


@router.post("/{project}/cron-jobs/{name}/run", response_model=CronFireOut, responses=REFUSALS)
async def run_cron_job(request: Request, project: ProjectName, name: Name, user: CurrentUser) -> CronFireOut:
    """Fire a cron job now: a fire due at this moment, which the hub handles as any fire."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        job = await cron_store.one(conn, access.project_id, name, lock=True)
        if job is None:
            _reader(access)
            raise _not_found(access, name)
        _manager(access, user, job, "firing")
        fire = await cron_store.add_fire(conn, job.id, datetime.now(UTC), manual=True, actor_id=user.user_id)
        if fire is None:  # the same microsecond as a fire before: a double press
            raise HTTPException(409, f"cron job {name} was fired at this moment already")
        await _record(conn, user, access, audit.CRON_RUN, name, job.revision)
    log.info("cron job fired now", extra={"project": project, "cron_job": name, "fire": fire["id"]})
    out = _fire(fire)
    assert out is not None
    return out


@router.delete("/{project}/cron-jobs/{name}", response_model=CronJobDeleted, responses=REFUSALS)
async def delete_cron_job(request: Request, project: ProjectName, name: Name, user: CurrentUser) -> CronJobDeleted:
    """Delete a cron job with its fires; its runs stay."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        job = await cron_store.one(conn, access.project_id, name, lock=True)
        if job is None:
            _reader(access)
            raise _not_found(access, name)
        _manager(access, user, job, "deleting")
        await cron_store.remove(conn, job.id)
        await _record(conn, user, access, audit.CRON_DELETE, name, job.revision)
    return CronJobDeleted(project=project, deleted=name)
