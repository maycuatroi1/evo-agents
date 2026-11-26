"""The SQL of the job of the night shift: the schedules due, and the lock of a schedule's row."""

from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import ColumnElement, and_, func, literal, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.feature_rule import feature_on
from evo_agents.hub.server.curator.models.charter import NIGHT_SHIFT
from evo_agents.hub.server.curator.repository.charter import zone_sql


def due_schedules(now: datetime | None):
    """Each night_shift schedule of a live project the Curator is on for, with its project, its owner's login, the
    newest revision of its charter, and the local time in the charter's zone at ``now`` (the database's now when None)."""
    s, c, p, u = tables.schedules, tables.charters, tables.projects, tables.users
    newest = (
        select(c.c.project_id, func.max(c.c.revision).label("revision")).group_by(c.c.project_id).subquery("newest")
    )
    moment = func.now() if now is None else literal(now)
    return (
        select(
            s.c.id,
            s.c.project_id,
            p.c.name.label("project"),
            s.c.owner_id,
            u.c.login.label("owner"),
            c.c.revision,
            c.c.body,
            func.timezone(zone_sql(c.c.body), moment).label("local_now"),
            moment.label("now"),
        )
        .join_from(s, p, p.c.id == s.c.project_id)
        .join(u, u.c.id == s.c.owner_id)
        .join(newest, newest.c.project_id == s.c.project_id)
        .join(c, and_(c.c.project_id == newest.c.project_id, c.c.revision == newest.c.revision))
        .where(
            s.c.kind == NIGHT_SHIFT,
            p.c.archived_at.is_(None),  # an archived project has no night shift (project_archive)
            cast(ColumnElement[bool], feature_on("curator", s.c.project_id)),
        )
        .order_by(s.c.id)
    )


async def all_due(conn: AsyncConnection, now: datetime | None):
    return (await conn.execute(due_schedules(now))).all()


async def due_of_project(conn: AsyncConnection, project_id: int):
    """The project's night_shift schedule as ``due_schedules`` reads it now; None without one."""
    return (await conn.execute(due_schedules(None).where(tables.schedules.c.project_id == project_id))).one_or_none()


async def lock_schedule(conn: AsyncConnection, schedule_id: int):
    """When the schedule was paused and last updated, its row locked; None when another transaction holds it."""
    s = tables.schedules
    locked = select(s.c.paused_at, s.c.updated_at).where(s.c.id == schedule_id).with_for_update(skip_locked=True)
    return (await conn.execute(locked)).one_or_none()
