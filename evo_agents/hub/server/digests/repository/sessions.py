"""The rows of session_digests: a push of a session's digest, the digests of a project, one of them, and those not
pushed again for a while."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.digests.models.sessions import SCAN_ROWS, DigestSummary, SessionDigest


def _summary_columns():
    d, u = tables.session_digests, tables.users
    return (
        d.c.session_id,
        u.c.login,
        d.c.cwd,
        d.c.messages,
        d.c.model,
        d.c.started_at,
        d.c.ended_at,
        d.c.label,
        d.c.created_at,
        d.c.updated_at,
    )


def _upsert(project_id: int, user_id: int, session_id: str, label: dict, body: SessionDigest):
    """The insert of a session's digest, which replaces the one held when the same member pushed it."""
    d = tables.session_digests
    values = {
        "label": label,
        "cwd": body.cwd,
        "messages": body.messages,
        "model": body.model,
        "started_at": body.started_at,
        "ended_at": body.ended_at,
        "body": body.model_dump(mode="json", by_alias=True),
    }
    stmt = pg_insert(d).values(project_id=project_id, session_id=session_id, user_id=user_id, **values)
    return stmt.on_conflict_do_update(
        constraint="session_digests_project_id_session_id_key",
        set_={**{name: stmt.excluded[name] for name in values}, "updated_at": func.now()},
        where=d.c.user_id == stmt.excluded.user_id,
    ).returning(d.c.id)


async def written(
    conn: AsyncConnection, project_id: int, user_id: int, session_id: str, label: dict, body: SessionDigest
) -> int | None:
    """Write the digest of session ``session_id``; its id, or None when another member pushed the session first."""
    found = (await conn.execute(_upsert(project_id, user_id, session_id, label, body))).first()
    return None if found is None else found.id


async def summary(conn: AsyncConnection, digest_id: int) -> DigestSummary:
    d, u = tables.session_digests, tables.users
    query = select(*_summary_columns()).join_from(d, u, u.c.id == d.c.user_id).where(d.c.id == digest_id)
    return DigestSummary(**(await conn.execute(query)).one()._mapping)


async def listed(conn: AsyncConnection, project_id: int, since: datetime | None, login: str | None) -> list:
    """The project's digests pushed at or after ``since`` by ``login``, the latest pushed first, at most SCAN_ROWS."""
    d, u = tables.session_digests, tables.users
    query = select(*_summary_columns()).join_from(d, u, u.c.id == d.c.user_id)
    query = query.where(d.c.project_id == project_id)
    if since is not None:
        query = query.where(d.c.updated_at >= since)
    if login is not None:
        query = query.where(func.lower(u.c.login) == login.lower())
    query = query.order_by(d.c.updated_at.desc(), d.c.id.desc()).limit(SCAN_ROWS)
    return list((await conn.execute(query)).all())


async def one(conn: AsyncConnection, project_id: int, session_id: str):
    """The project's digest of session ``session_id``, with its body; or None."""
    d, u = tables.session_digests, tables.users
    query = (
        select(*_summary_columns(), d.c.body)
        .join_from(d, u, u.c.id == d.c.user_id)
        .where(d.c.project_id == project_id, d.c.session_id == session_id)
    )
    return (await conn.execute(query)).first()


async def pruned(conn: AsyncConnection, days: int) -> int:
    """Delete the digests not pushed again for ``days`` days; how many."""
    d = tables.session_digests
    return (await conn.execute(delete(d).where(d.c.updated_at < func.now() - timedelta(days=days)))).rowcount
