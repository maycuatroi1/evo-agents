"""The registry of a project's signal keys and its ingest keys in the database: tables signal_keys and
signal_ingest_keys. The signals themselves are read and written by ``evo_agents.hub.signals``."""

from __future__ import annotations

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.base.errors import Conflict

UNIQUE_VIOLATION = "23505"
TAKEN = "another write took this name at the same moment: list the keys and try again"


async def _taken(conn: AsyncConnection, statement):
    """``statement``'s row, or 409 when a write at the same moment took its unique name."""
    try:
        async with conn.begin_nested():
            return (await conn.execute(statement)).one()
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) != UNIQUE_VIOLATION:
            raise
        raise Conflict(TAKEN) from None


# The registry of keys


def _keys_query(project_id: int):
    sk, users = tables.signal_keys, tables.users
    return (
        select(
            sk.c.name,
            sk.c.kind,
            sk.c.unit,
            sk.c.description,
            sk.c.source,
            sk.c.level,
            users.c.login.label("created_by"),
            sk.c.created_at,
            sk.c.archived_at,
        )
        .select_from(sk.join(users, users.c.id == sk.c.created_by))
        .where(sk.c.project_id == project_id)
        .order_by(sk.c.name)
    )


async def keys(conn: AsyncConnection, project_id: int, archived: bool) -> list:
    """The keys of the project's registry, by name; the archived ones too with ``archived``."""
    query = _keys_query(project_id)
    if not archived:
        query = query.where(tables.signal_keys.c.archived_at.is_(None))
    return list((await conn.execute(query)).all())


async def key(conn: AsyncConnection, project_id: int, name: str):
    """Key ``name`` of the project's registry; None without one."""
    query = _keys_query(project_id).where(tables.signal_keys.c.name == name)
    return (await conn.execute(query)).one_or_none()


async def key_held(conn: AsyncConnection, project_id: int, name: str):
    """When key ``name`` of the project was archived, as a row; None when the registry has no such key."""
    sk = tables.signal_keys
    query = select(sk.c.archived_at).where(sk.c.project_id == project_id, sk.c.name == name)
    return (await conn.execute(query)).one_or_none()


async def add_key(conn: AsyncConnection, project_id: int, user_id: int, values: dict) -> None:
    """Write a key of the project's registry; 409 when a write at the same moment took its name."""
    sk = tables.signal_keys
    statement = insert(sk).values(project_id=project_id, created_by=user_id, **values)
    await _taken(conn, statement.returning(sk.c.id))


async def archive_key(conn: AsyncConnection, project_id: int, name: str, user_id: int) -> bool:
    """Archive key ``name`` of the project; whether it was live until now."""
    sk = tables.signal_keys
    found = await conn.execute(
        update(sk)
        .where(sk.c.project_id == project_id, sk.c.name == name, sk.c.archived_at.is_(None))
        .values(archived_at=func.now(), archived_by=user_id)
        .returning(sk.c.id)
    )
    return found.one_or_none() is not None


# Ingest keys


def _ingest_query(project_id: int):
    ik, users = tables.signal_ingest_keys, tables.users
    revoker = users.alias("revoker")
    return (
        select(
            ik.c.id,
            ik.c.name,
            ik.c.max_level,
            users.c.login.label("created_by"),
            ik.c.created_at,
            ik.c.last_used_at,
            ik.c.revoked_at,
            revoker.c.login.label("revoked_by"),
        )
        .select_from(ik.join(users, users.c.id == ik.c.created_by).outerjoin(revoker, revoker.c.id == ik.c.revoked_by))
        .where(ik.c.project_id == project_id)
        .order_by(ik.c.id)
    )


async def ingest_keys(conn: AsyncConnection, project_id: int, revoked: bool) -> list:
    """The project's ingest keys, oldest first; the revoked ones too with ``revoked``."""
    query = _ingest_query(project_id)
    if not revoked:
        query = query.where(tables.signal_ingest_keys.c.revoked_at.is_(None))
    return list((await conn.execute(query)).all())


async def ingest_key(conn: AsyncConnection, project_id: int, key_id: int):
    """Ingest key ``key_id`` of the project; None without one."""
    query = _ingest_query(project_id).where(tables.signal_ingest_keys.c.id == key_id)
    return (await conn.execute(query)).one_or_none()


async def live_ingest_key(conn: AsyncConnection, project_id: int, name: str) -> bool:
    """Whether the project has an ingest key ``name`` that is not revoked."""
    ik = tables.signal_ingest_keys
    live = select(ik.c.id).where(ik.c.project_id == project_id, ik.c.name == name, ik.c.revoked_at.is_(None))
    return (await conn.execute(live)).first() is not None


async def add_ingest_key(conn: AsyncConnection, user_id: int, values: dict, token_hash: str) -> int:
    """Write an ingest key; its id. 409 when a write at the same moment took its name."""
    ik = tables.signal_ingest_keys
    statement = insert(ik).values(**values, token_hash=token_hash, created_by=user_id)
    return (await _taken(conn, statement.returning(ik.c.id)))[0]


async def revoke_ingest_key(conn: AsyncConnection, project_id: int, key_id: int, user_id: int) -> str | None:
    """Revoke ingest key ``key_id`` of the project; its name when it was live until now, None otherwise."""
    ik = tables.signal_ingest_keys
    revoked = await conn.execute(
        update(ik)
        .where(ik.c.project_id == project_id, ik.c.id == key_id, ik.c.revoked_at.is_(None))
        .values(revoked_at=func.now(), revoked_by=user_id)
        .returning(ik.c.name)
    )
    return revoked.scalar_one_or_none()
