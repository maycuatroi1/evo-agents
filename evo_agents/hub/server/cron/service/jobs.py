"""The cron jobs of a project, as ``cron.routes`` says: who may read, change and manage a job, what a job starts, and
each read and write of the routes.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import cron, cron_store
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound, Unprocessable
from evo_agents.hub.server.cron import repository as store
from evo_agents.hub.server.cron.models import CronFireOut, CronJobOut, CronJobPut, CronJobWritten
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30  # minutes a command may run
MAX_TIMEOUT = 240
MAX_PROMPT = 20_000
MAX_COMMAND = 4_000
COST_WINDOW = timedelta(hours=24)


class RevisionConflict(Exception):
    """A write that names a revision the job is not at: 409 ``revision_conflict``, with the job as the hub holds it."""

    def __init__(self, message: str, current: CronJobOut):
        super().__init__(message)
        self.message, self.current = message, current


# Who may


def _reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise Forbidden(f"reading the cron jobs of project {access.name} needs a grant on it")


def _writer(access: ProjectAccess, doing: str) -> None:
    if not has_role(access.role, "writer"):
        held = access.role or "no grant"
        raise Forbidden(f"{doing} a cron job of project {access.name} needs the writer role on it; you hold {held}")


def can_change(access: ProjectAccess, user: Principal, job: cron_store.Job) -> bool:
    return has_role(access.role, "writer") and job.owner_id == user.user_id


def can_manage(access: ProjectAccess, user: Principal, job: cron_store.Job) -> bool:
    return can_change(access, user, job) or has_role(access.role, "admin")


def _manager(access: ProjectAccess, user: Principal, job: cron_store.Job, doing: str) -> None:
    if not can_manage(access, user, job):
        raise Forbidden(
            f"{doing} cron job {job.name} of project {access.name} needs its owner ({job.owner}) with the writer role, "
            "or the admin role on the project"
        )


def _not_found(access: ProjectAccess, name: str) -> NotFound:
    return NotFound(
        f"project {access.name} has no cron job {name}: see `evo-agents hub cron list --project {access.name}`"
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
    return await store.costs(conn, job_ids, datetime.now(UTC) - COST_WINDOW)


async def _shown(conn, access, user, job: cron_store.Job) -> CronJobOut:
    last = await cron_store.last_fires(conn, [job.id])
    cost = await _costs(conn, [job.id])
    return _out(access, user, job, last.get(job.id), cost.get(job.id, 0.0))


async def _record(conn, user: Principal, access: ProjectAccess, action: str, job_name: str, revision: int) -> None:
    await Auditor.of(conn, user, access).record(action, f"{access.name} cron={job_name} revision={revision}")


# What a job starts


def _blank(value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _only(body: CronJobPut, allowed: tuple[str, ...]) -> None:
    given = [
        key
        for key in ("agent", "chat_id", "prompt", "plan", "command", "repo", "timeout_minutes")
        if getattr(body, key) is not None and key not in allowed
    ]
    if given:
        raise Unprocessable(f"a job of kind {body.kind} takes no {', '.join(given)}: those belong to another kind")


async def _agent_work(conn, access: ProjectAccess, user: Principal, body: CronJobPut) -> dict:
    _only(body, ("agent", "chat_id", "prompt"))
    handle, prompt = _blank(body.agent), _blank(body.prompt)
    if handle is None or prompt is None:
        raise Unprocessable("a job of kind agent needs agent, the handle of an agent of the project, and prompt")
    if len(prompt) > MAX_PROMPT:
        raise Unprocessable(f"the prompt is {len(prompt)} characters; a job takes at most {MAX_PROMPT}")
    found = await store.agent_id(conn, access.project_id, handle.lstrip("@"))
    if found is None:
        raise Unprocessable(
            f"project {access.name} has no agent @{handle.lstrip('@')}: see `evo-agents hub agent list --project "
            f"{access.name}`"
        )
    if body.chat_id is not None:
        if not await store.in_chat(conn, body.chat_id, user.user_id):
            raise Unprocessable(f"you are not in chat {body.chat_id}: a job posts only in a chat of yours")
    return {"profile_id": found, "chat_id": body.chat_id, "prompt": prompt}


async def _plan_work(conn, access: ProjectAccess, body: CronJobPut) -> dict:
    _only(body, ("plan",))
    plan = _blank(body.plan)
    if plan is None:
        raise Unprocessable("a job of kind plan needs plan, a plan of the project")
    if not await store.has_plan(conn, access.project_id, plan):
        raise Unprocessable(
            f"project {access.name} has no plan {plan}: see `evo-agents hub plan list --project {access.name}`"
        )
    return {"plan_id": plan}


async def _command_work(conn, access: ProjectAccess, body: CronJobPut) -> dict:
    _only(body, ("command", "repo", "timeout_minutes"))
    command = _blank(body.command)
    if command is None:
        raise Unprocessable("a job of kind command needs command, the shell command line it runs")
    if len(command) > MAX_COMMAND:
        raise Unprocessable(f"the command is {len(command)} characters; a job takes at most {MAX_COMMAND}")
    timeout = DEFAULT_TIMEOUT if body.timeout_minutes is None else body.timeout_minutes
    if not 1 <= timeout <= MAX_TIMEOUT:
        raise Unprocessable(f"timeout_minutes is {timeout}: a command runs 1 to {MAX_TIMEOUT} minutes")
    repo = _blank(body.repo)
    if repo is not None:
        if not await store.has_repo(conn, access.project_id, repo):
            raise Unprocessable(
                f"project {access.name} has no repo {repo}: a command runs in a repo of the project or none"
            )
    return {"command": command, "repo": repo, "timeout_minutes": timeout}


async def checked(conn, access: ProjectAccess, user: Principal, body: CronJobPut) -> dict:
    """The columns a write of ``body`` stores: cron, timezone, kind and every column of ``cron_store.WORK``; 422 with
    the reason when the hub does not take it."""
    try:
        expr = cron.check(body.cron, body.timezone, body.kind)
    except cron.CronRefused as exc:
        raise Unprocessable(str(exc)) from None
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


async def listed(conn: AsyncConnection, user: Principal, project: str) -> list[CronJobOut]:
    """The cron jobs of the project, by name, with their next fire, last fire and cost in 24 hours."""
    access = await project_access(conn, user, project)
    _reader(access)
    jobs = await cron_store.listed(conn, access.project_id)
    ids = [job.id for job in jobs]
    last = await cron_store.last_fires(conn, ids)
    costs = await _costs(conn, ids)
    return [_out(access, user, job, last.get(job.id), costs.get(job.id, 0.0)) for job in jobs]


async def _readable(conn: AsyncConnection, user: Principal, project: str, name: str):
    access = await project_access(conn, user, project)
    _reader(access)
    job = await cron_store.one(conn, access.project_id, name)
    if job is None:
        raise _not_found(access, name)
    return access, job


async def one(conn: AsyncConnection, user: Principal, project: str, name: str) -> CronJobOut:
    access, job = await _readable(conn, user, project, name)
    return await _shown(conn, access, user, job)


async def fires(conn: AsyncConnection, user: Principal, project: str, name: str, limit: int) -> list[CronFireOut]:
    """The fires of the job, the latest due first."""
    _, job = await _readable(conn, user, project, name)
    found = await cron_store.fires(conn, job.id, limit)
    return [fire for fire in map(_fire, found) if fire is not None]


# Changing


async def put(conn: AsyncConnection, user: Principal, project: str, name: str, body: CronJobPut) -> CronJobWritten:
    """Create the job, owned by ``user``, or store what it starts as its next revision; RevisionConflict when
    ``body.if_revision`` is not its revision."""
    access = await project_access(conn, user, project)
    _writer(access, "changing")
    held = await cron_store.one(conn, access.project_id, name, lock=True)
    if held is None and body.if_revision:
        raise NotFound(f"project {access.name} has no cron job {name} to replace")
    if held is not None and not can_change(access, user, held):
        raise Forbidden(
            f"cron job {name} is {held.owner}'s: what it starts runs as them, so only they change it; an admin "
            "may pause, resume, fire or delete it"
        )
    if held is not None and body.if_revision != held.revision:
        if not body.if_revision:
            message = f"cron job {name} exists at revision {held.revision}: pass if_revision {held.revision}"
        else:
            message = (
                f"cron job {name} is at revision {held.revision} on the hub, not {body.if_revision}: someone "
                "changed it since you read it. Read it again and retry; nothing was written"
            )
        raise RevisionConflict(message, await _shown(conn, access, user, held))
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
    return CronJobWritten(**out.model_dump(), created=held is None, changed=True)


async def _managed(conn: AsyncConnection, user: Principal, project: str, name: str, doing: str):
    """(access, job), the job locked, once ``user`` may manage it."""
    access = await project_access(conn, user, project)
    job = await cron_store.one(conn, access.project_id, name, lock=True)
    if job is None:
        _reader(access)
        raise _not_found(access, name)
    _manager(access, user, job, doing)
    return access, job


async def _change(
    conn: AsyncConnection,
    user: Principal,
    project: str,
    name: str,
    doing: str,
    change: Callable[[cron_store.Job], Awaitable[str]],
) -> CronJobOut:
    access, job = await _managed(conn, user, project, name, doing)
    action = await change(job)
    await _record(conn, user, access, action, name, job.revision)
    held = await cron_store.one(conn, access.project_id, name)
    assert held is not None
    return await _shown(conn, access, user, held)


async def pause(conn: AsyncConnection, user: Principal, project: str, name: str, reason: str | None) -> CronJobOut:
    async def change(job):
        await cron_store.pause(conn, job.id, user.user_id, _blank(reason))
        return audit.CRON_PAUSE

    return await _change(conn, user, project, name, "pausing", change)


async def resume(conn: AsyncConnection, user: Principal, project: str, name: str) -> CronJobOut:
    async def change(job):
        await cron_store.resume(conn, job.id)
        return audit.CRON_RESUME

    return await _change(conn, user, project, name, "resuming", change)


async def fire_now(conn: AsyncConnection, user: Principal, project: str, name: str) -> dict:
    """Add a fire due at this moment; the fire's row."""
    access, job = await _managed(conn, user, project, name, "firing")
    fire = await cron_store.add_fire(conn, job.id, datetime.now(UTC), manual=True, actor_id=user.user_id)
    if fire is None:  # the same microsecond as a fire before: a double press
        raise Conflict(f"cron job {name} was fired at this moment already")
    await _record(conn, user, access, audit.CRON_RUN, name, job.revision)
    return fire


def fired(project: str, name: str, fire: dict) -> CronFireOut:
    """What run now answers once its fire is written."""
    log.info("cron job fired now", extra={"project": project, "cron_job": name, "fire": fire["id"]})
    out = _fire(fire)
    assert out is not None
    return out


def stored(project: str, name: str, written: CronJobWritten) -> None:
    if written.changed:
        log.info("cron job stored", extra={"project": project, "cron_job": name, "revision": written.revision})


async def delete(conn: AsyncConnection, user: Principal, project: str, name: str) -> None:
    """Delete the job with its fires; its runs stay."""
    access, job = await _managed(conn, user, project, name, "deleting")
    await cron_store.remove(conn, job.id)
    await _record(conn, user, access, audit.CRON_DELETE, name, job.revision)
