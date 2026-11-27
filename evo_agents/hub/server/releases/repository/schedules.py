"""The SQL of the release schedules (``evo_agents.hub.releases``): each with its project, its owner's and pauser's
logins and its local time, the moment of its next check, the time zones Postgres knows, the project's repos on GitHub,
and setting, pausing and resuming one."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Text, case, cast, column, exists, func, insert, literal, select, table, update
from sqlalchemy.dialects.postgresql import INTERVAL
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.credentials import github_repo

PG_TIMEZONES = table("pg_timezone_names", column("name", Text))


def schedules(now: datetime | None = None):
    """Each release schedule with its project's name, its owner's and pauser's logins, and the local time in its zone
    at ``now`` (the database's now when None)."""
    s, p, u = tables.release_schedules, tables.projects, tables.users
    pauser = u.alias("pauser")
    moment = func.now() if now is None else literal(now)
    return (
        select(
            s,
            p.c.name.label("project"),
            u.c.login.label("owner"),
            pauser.c.login.label("paused_by_login"),
            func.timezone(s.c.timezone, moment).label("local_now"),
        )
        .join_from(s, p, p.c.id == s.c.project_id)
        .join(u, u.c.id == s.c.owner_id)
        .outerjoin(pauser, pauser.c.id == s.c.paused_by)
        .order_by(s.c.id)
    )


async def every_schedule(conn: AsyncConnection, now: datetime | None = None) -> list:
    return list((await conn.execute(schedules(now))).all())


async def schedule_of(conn: AsyncConnection, schedule_id: int, now: datetime | None = None):
    """Schedule ``schedule_id`` as ``schedules`` reads it at ``now``, or None."""
    query = schedules(now).where(tables.release_schedules.c.id == schedule_id)
    return (await conn.execute(query)).one_or_none()


async def lock_schedule(conn: AsyncConnection, schedule_id: int) -> None:
    s = tables.release_schedules
    await conn.execute(select(s.c.id).where(s.c.id == schedule_id).with_for_update())


async def project_schedule(conn: AsyncConnection, project_id: int, *, lock: bool = False):
    query = schedules().where(tables.release_schedules.c.project_id == project_id)
    if lock:
        query = query.with_for_update(of=tables.release_schedules)
    return (await conn.execute(query)).one_or_none()


def _next_check(row):
    """The next moment, in UTC, whose local time in the schedule's zone is its check_at."""
    local_now = func.timezone(row.timezone, func.now())
    today = func.date_trunc("day", local_now) + cast(literal(row.check_at), INTERVAL)
    upcoming = case((today > local_now, today), else_=today + timedelta(days=1))
    return func.timezone(row.timezone, upcoming)


async def next_check_at(conn: AsyncConnection, row) -> datetime | None:
    return (await conn.execute(select(_next_check(row)))).scalar_one()


async def login_of(conn: AsyncConnection, user_id: int) -> str:
    u = tables.users
    return (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()


async def origin(conn: AsyncConnection, project_id: int, repo: str) -> str | None:
    r = tables.project_repos
    query = select(r.c.origin).where(r.c.project_id == project_id, r.c.name == repo)
    return (await conn.execute(query)).scalar_one_or_none()


async def github_repos(conn: AsyncConnection, project_id: int) -> dict[str, str]:
    """{name: owner/name} of the project's repos on GitHub."""
    r = tables.project_repos
    rows = (await conn.execute(select(r.c.name, r.c.origin).where(r.c.project_id == project_id))).all()
    return {row.name: "/".join(found) for row in rows if row.origin and (found := github_repo(row.origin))}


async def time_zone_known(conn: AsyncConnection, name: str) -> bool:
    return (await conn.execute(select(exists().where(PG_TIMEZONES.c.name == name)))).scalar_one()


async def insert_schedule(conn: AsyncConnection, project_id: int, values: dict) -> None:
    await conn.execute(insert(tables.release_schedules).values(project_id=project_id, **values))


async def update_schedule(conn: AsyncConnection, schedule_id: int, values: dict) -> None:
    s = tables.release_schedules
    await conn.execute(update(s).values(**values, updated_at=func.now()).where(s.c.id == schedule_id))


async def pause(conn: AsyncConnection, schedule_id: int, user_id: int) -> None:
    """Pause schedule ``schedule_id`` as ``user_id``; a second pause keeps the first one's time."""
    s = tables.release_schedules
    await conn.execute(
        update(s)
        .values(paused_at=func.now(), paused_by=user_id, updated_at=func.now())
        .where(s.c.id == schedule_id, s.c.paused_at.is_(None))
    )


async def resume(conn: AsyncConnection, schedule_id: int) -> None:
    s = tables.release_schedules
    await conn.execute(
        update(s).values(paused_at=None, paused_by=None, updated_at=func.now()).where(s.c.id == schedule_id)
    )
