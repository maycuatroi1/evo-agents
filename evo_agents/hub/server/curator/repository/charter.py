"""The SQL of a project's charter: its revisions with their writers, the time zones Postgres knows, and the project's
night shift schedule the charter keeps."""

from __future__ import annotations

from sqlalchemy import Text, and_, column, exists, func, insert, select, table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.curator.models.charter import MAX_CHARTER_REVISIONS, NIGHT_SHIFT

PG_TIMEZONES = table("pg_timezone_names", column("name", Text))


def zone_sql(body):
    """The time zone of a charter's ``body`` (a JSONB column) in SQL: its window's, else its pacing's
    (``curator.zone_of``)."""
    return func.coalesce(body["window"]["timezone"].astext, body["pacing"]["timezone"].astext)


def charters():
    """A charter revision with its writer's login and the login of the owner of the project's night shift schedule."""
    c, s, writer, owner = tables.charters, tables.schedules, tables.users.alias("writer"), tables.users.alias("owner")
    return (
        select(
            c.c.project_id,
            c.c.revision,
            c.c.body,
            c.c.created_at.label("updated_at"),
            writer.c.login.label("updated_by"),
            func.coalesce(owner.c.login, writer.c.login).label("schedule_owner"),
        )
        .join_from(c, writer, writer.c.id == c.c.created_by)
        .outerjoin(s, and_(s.c.project_id == c.c.project_id, s.c.kind == NIGHT_SHIFT))
        .outerjoin(owner, owner.c.id == s.c.owner_id)
    )


async def charter_row(conn: AsyncConnection, project_id: int, revision: int | None = None):
    c = tables.charters
    query = charters().where(c.c.project_id == project_id)
    if revision is not None:
        query = query.where(c.c.revision == revision)
    return (await conn.execute(query.order_by(c.c.revision.desc()).limit(1))).one_or_none()


async def revisions(conn: AsyncConnection, project_id: int):
    """The project's charter revisions, newest first, MAX_CHARTER_REVISIONS at most."""
    c = tables.charters
    query = charters().where(c.c.project_id == project_id).order_by(c.c.revision.desc())
    return (await conn.execute(query.limit(MAX_CHARTER_REVISIONS))).all()


async def time_zone_known(conn: AsyncConnection, name: str) -> bool:
    return (await conn.execute(select(exists().where(PG_TIMEZONES.c.name == name)))).scalar_one()


async def pool_owner(conn: AsyncConnection, pool_id: int) -> int:
    p = tables.worker_pools
    return (await conn.execute(select(p.c.owner_id).where(p.c.id == pool_id))).scalar_one()


async def keep_schedule(conn: AsyncConnection, project_id: int, owner_id: int) -> None:
    s = tables.schedules
    statement = pg_insert(s).values(project_id=project_id, kind=NIGHT_SHIFT, owner_id=owner_id)
    statement = statement.on_conflict_do_update(
        constraint="schedules_project_id_kind_key",
        set_={"owner_id": owner_id, "updated_at": func.now()},
        where=s.c.owner_id.is_distinct_from(owner_id),
    )
    await conn.execute(statement)


async def insert_revision(conn: AsyncConnection, values: dict) -> None:
    await conn.execute(insert(tables.charters).values(**values))
