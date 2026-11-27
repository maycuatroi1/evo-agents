"""The SQL of the checks of a release schedule: the latest ones with the login of who asked, whether the scheduled
check of a day is made, and recording one."""

from __future__ import annotations

from datetime import date

from sqlalchemy import exists, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import releases, tables


def _checks(schedule_id: int, limit: int):
    k, u = tables.release_checks, tables.users
    return (
        select(k, u.c.login.label("requested_login"))
        .outerjoin(u, u.c.id == k.c.requested_by)
        .where(k.c.schedule_id == schedule_id)
        .order_by(k.c.id.desc())
        .limit(limit)
    )


async def latest_checks(conn: AsyncConnection, schedule_id: int, limit: int) -> list:
    """The latest ``limit`` checks of ``schedule_id``, newest first, with ``requested_login``."""
    return list((await conn.execute(_checks(schedule_id, limit))).all())


async def check_row(conn: AsyncConnection, schedule_id: int, check_id: int):
    """Check ``check_id`` of ``schedule_id`` as ``latest_checks`` reads it."""
    k = tables.release_checks
    return (await conn.execute(_checks(schedule_id, 1).where(k.c.id == check_id))).one()


async def day_done(conn: AsyncConnection, schedule_id: int, day: date | None) -> bool:
    """Whether the scheduled check of ``day`` is made: one that did not fail, or ERROR_TRIES that did."""
    k = tables.release_checks
    of_day = (k.c.schedule_id == schedule_id, k.c.trigger == "schedule", k.c.local_day == day)
    query = select(
        exists().where(*of_day, k.c.outcome != "error").label("made"),
        select(func.count()).select_from(k).where(*of_day, k.c.outcome == "error").scalar_subquery().label("failed"),
    )
    found = (await conn.execute(query)).one()
    return bool(found.made) or found.failed >= releases.ERROR_TRIES


async def insert_check(conn: AsyncConnection, values: dict) -> int:
    """Record a check of ``values``; its id."""
    k = tables.release_checks
    return (await conn.execute(insert(k).values(**values).returning(k.c.id))).scalar_one()
