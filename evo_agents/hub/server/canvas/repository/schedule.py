"""The SQL of the weekly baselines' schedule: whether Postgres knows a time zone, the body of a project's latest
charter, the time in a zone by Postgres's clock, and whether a canvas has the baseline of a week.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Text, column, func, literal, select, table
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables

PG_TIMEZONES = table("pg_timezone_names", column("name", Text))  # evo_agents.hub.server.curator.PG_TIMEZONES


async def known_zone(conn: AsyncConnection, zone: str) -> bool:
    """Whether Postgres knows time zone ``zone``."""
    return await conn.scalar(select(PG_TIMEZONES.c.name).where(PG_TIMEZONES.c.name == zone).limit(1)) is not None


async def charter_body(conn: AsyncConnection, project_id: int) -> dict | None:
    """The body of the latest revision of project ``project_id``'s charter; None when it has none."""
    c = tables.charters
    query = select(c.c.body).where(c.c.project_id == project_id).order_by(c.c.revision.desc()).limit(1)
    return await conn.scalar(query)


async def local_now(conn: AsyncConnection, zone: str) -> datetime:
    """The time it is in ``zone`` by Postgres's clock, without a zone."""
    found = await conn.scalar(select(func.timezone(zone, func.now())))
    if found is None:
        raise RuntimeError("Postgres gave no time")
    return found


async def in_utc(conn: AsyncConnection, zone: str, moment: datetime) -> datetime:
    """The local ``moment`` of ``zone`` as a time with a zone."""
    found = await conn.scalar(select(func.timezone(zone, literal(moment))))
    if found is None:
        raise RuntimeError("Postgres gave no time")
    return found


async def has_week(conn: AsyncConnection, canvas_id: int, week: str) -> bool:
    """Whether canvas ``canvas_id`` has a baseline of ISO week ``week``."""
    b = tables.canvas.baselines
    return await conn.scalar(select(b.c.id).where(b.c.canvas_id == canvas_id, b.c.week == week)) is not None
