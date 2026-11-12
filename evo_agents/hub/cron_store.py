"""The rows of a project's cron jobs and their fires (``evo_agents.hub.tables_cron``; ``docs/hub.md``, Cron jobs): one
job as the hub holds it now, a project's jobs, writing a job by revision, its pause, its fires and the last fire of
each job. The routes of ``evo_agents.hub.server.cron_jobs`` check who may; these only read and write, SQLAlchemy Core
on ``evo_agents.hub.tables``.

Outside ``evo_agents.hub.server``: the hub's job worker, which fires the jobs, imports it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.orm import aliased

from evo_agents.hub import tables

WORK = ("profile_id", "chat_id", "prompt", "plan_id", "command", "repo", "timeout_minutes")  # what a kind starts
FIRE_KEYS = ("id", "scheduled_for", "manual", "outcome", "reason", "run_id", "created_at", "handled_at")


@dataclass(frozen=True)
class Job:
    id: int
    project_id: int
    project: str
    name: str
    cron: str
    timezone: str
    kind: str
    profile_id: int | None
    agent: str | None  # the handle of the profile
    chat_id: int | None
    prompt: str | None
    plan_id: str | None
    command: str | None
    repo: str | None
    timeout_minutes: int | None
    owner_id: int
    owner: str  # the owner's login
    revision: int
    updated_by: str | None
    created_at: datetime
    updated_at: datetime
    paused_at: datetime | None
    paused_by: str | None
    paused_reason: str | None

    def work(self) -> dict:
        """What the job starts, as a write takes it: the columns of WORK."""
        return {key: getattr(self, key) for key in WORK}


def _select() -> Select:
    j, p, ap = tables.cron_jobs, tables.projects, tables.agent_profiles
    owner, changer, pauser = aliased(tables.users), aliased(tables.users), aliased(tables.users)
    return (
        select(
            j.c.id,
            j.c.project_id,
            p.c.name.label("project"),
            j.c.name,
            j.c.cron,
            j.c.timezone,
            j.c.kind,
            j.c.profile_id,
            ap.c.handle.label("agent"),
            j.c.chat_id,
            j.c.prompt,
            j.c.plan_id,
            j.c.command,
            j.c.repo,
            j.c.timeout_minutes,
            j.c.owner_id,
            owner.c.login.label("owner"),
            j.c.revision,
            changer.c.login.label("updated_by"),
            j.c.created_at,
            j.c.updated_at,
            j.c.paused_at,
            pauser.c.login.label("paused_by"),
            j.c.paused_reason,
        )
        .join_from(j, p, p.c.id == j.c.project_id)
        .join(owner, owner.c.id == j.c.owner_id)
        .outerjoin(ap, ap.c.id == j.c.profile_id)
        .outerjoin(changer, changer.c.id == j.c.updated_by)
        .outerjoin(pauser, pauser.c.id == j.c.paused_by)
    )


def _job(row) -> Job:
    return Job(**row._mapping)


async def one(conn: AsyncConnection, project_id: int, name: str, *, lock: bool = False) -> Job | None:
    """Job ``name`` of project ``project_id``; with ``lock`` its row is locked for the transaction."""
    j = tables.cron_jobs
    query = _select().where(j.c.project_id == project_id, j.c.name == name)
    if lock:
        query = query.with_for_update(of=j)
    row = (await conn.execute(query)).one_or_none()
    return None if row is None else _job(row)


async def listed(conn: AsyncConnection, project_id: int) -> list[Job]:
    """The jobs of project ``project_id``, by name."""
    j = tables.cron_jobs
    rows = await conn.execute(_select().where(j.c.project_id == project_id).order_by(j.c.name))
    return [_job(row) for row in rows]


async def write(
    conn: AsyncConnection,
    project_id: int,
    name: str,
    fields: dict,
    user_id: int,
    held: Job | None,
) -> int:
    """Create job ``name`` owned by ``user_id`` (``held`` None) or store ``fields`` as its next revision; the revision.
    ``fields`` holds cron, timezone, kind and every column of WORK. The caller locked ``held`` and checked the
    revision."""
    j = tables.cron_jobs
    if held is None:
        values = {**fields, "project_id": project_id, "name": name, "owner_id": user_id, "updated_by": user_id}
        await conn.execute(insert(j).values(**values))
        return 1
    revision = held.revision + 1
    await conn.execute(
        update(j)
        .where(j.c.id == held.id)
        .values(**fields, revision=revision, updated_by=user_id, updated_at=func.now())
    )
    return revision


async def pause(conn: AsyncConnection, job_id: int, by: int | None, reason: str | None) -> None:
    """Pause job ``job_id``: by ``by`` (None: the hub), for ``reason``. A paused job does not fire."""
    j = tables.cron_jobs
    await conn.execute(
        update(j).where(j.c.id == job_id).values(paused_at=func.now(), paused_by=by, paused_reason=reason)
    )


async def resume(conn: AsyncConnection, job_id: int) -> None:
    j = tables.cron_jobs
    await conn.execute(update(j).where(j.c.id == job_id).values(paused_at=None, paused_by=None, paused_reason=None))


async def remove(conn: AsyncConnection, job_id: int) -> None:
    """Delete job ``job_id`` with its fires; its runs stay, their link cleared."""
    await conn.execute(delete(tables.cron_jobs).where(tables.cron_jobs.c.id == job_id))


def _fire_select() -> Select:
    f, actor = tables.cron_fires, aliased(tables.users)
    return select(f.c.job_id, *(f.c[key] for key in FIRE_KEYS), actor.c.login.label("actor")).outerjoin(
        actor, actor.c.id == f.c.actor_id
    )


async def add_fire(
    conn: AsyncConnection, job_id: int, scheduled_for: datetime, *, manual: bool = False, actor_id: int | None = None
) -> dict | None:
    """Record a fire of job ``job_id`` due at ``scheduled_for``, its outcome not yet known; None when that job has a
    fire at that minute already (``cron_fires_job_id_scheduled_for_key``)."""
    f = tables.cron_fires
    query = (
        pg_insert(f)
        .values(job_id=job_id, scheduled_for=scheduled_for, manual=manual, actor_id=actor_id)
        .on_conflict_do_nothing(constraint="cron_fires_job_id_scheduled_for_key")
        .returning(f.c.id)
    )
    fire_id = (await conn.execute(query)).scalar_one_or_none()
    if fire_id is None:
        return None
    return dict((await conn.execute(_fire_select().where(f.c.id == fire_id))).one()._mapping)


async def fires(conn: AsyncConnection, job_id: int, limit: int) -> list[dict]:
    """The fires of job ``job_id``, the latest due first, at most ``limit``."""
    f = tables.cron_fires
    query = _fire_select().where(f.c.job_id == job_id).order_by(f.c.scheduled_for.desc(), f.c.id.desc()).limit(limit)
    return [dict(row._mapping) for row in await conn.execute(query)]


async def last_fires(conn: AsyncConnection, job_ids: list[int]) -> dict[int, dict]:
    """job id -> its latest fire, for those of ``job_ids`` that fired."""
    if not job_ids:
        return {}
    f = tables.cron_fires
    query = (
        _fire_select()
        .where(f.c.job_id.in_(job_ids))
        .distinct(f.c.job_id)
        .order_by(f.c.job_id, f.c.scheduled_for.desc(), f.c.id.desc())
    )
    return {row.job_id: dict(row._mapping) for row in await conn.execute(query)}
