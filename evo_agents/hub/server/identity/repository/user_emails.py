"""The SQL of a member's emails (``evo_agents.hub.server.user_emails``)."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


def listed():
    """Every address as UserEmail reads it, with its member's id."""
    e, users = tables.user_emails, tables.users
    return select(e.c.id, e.c.email, users.c.login, e.c.created_at, e.c.user_id).join_from(
        e, users, users.c.id == e.c.user_id
    )


async def member_row(conn: AsyncConnection, user_id: int | None, login: str | None, *, lock: bool = False):
    """(id, login) of the member of id ``user_id``, else of ``login`` in any case, or None. ``lock`` holds the
    member's row until the transaction ends."""
    users = tables.users
    named = users.c.id == user_id if user_id is not None else func.lower(users.c.login) == (login or "").lower()
    query = select(users.c.id, users.c.login).where(named)
    return (await conn.execute(query.with_for_update(key_share=True) if lock else query)).one_or_none()


async def emails_of(conn: AsyncConnection, user_id: int):
    """The addresses of member ``user_id``, in the order of the addresses."""
    e = tables.user_emails
    return (await conn.execute(listed().where(e.c.user_id == user_id).order_by(func.lower(e.c.email), e.c.id))).all()


async def holder(conn: AsyncConnection, email: str):
    """The row of ``email`` in any case, or None."""
    e = tables.user_emails
    return (await conn.execute(listed().where(func.lower(e.c.email) == func.lower(email)))).one_or_none()


async def count(conn: AsyncConnection, user_id: int) -> int:
    e = tables.user_emails
    return (await conn.execute(select(func.count()).select_from(e).where(e.c.user_id == user_id))).scalar_one()


async def add(conn: AsyncConnection, user_id: int, email: str):
    """(id, email, created_at) of the new row of ``email``, or None when another member took it since."""
    e = tables.user_emails
    added = pg_insert(e).values(user_id=user_id, email=email).on_conflict_do_nothing()
    return (await conn.execute(added.returning(e.c.id, e.c.email, e.c.created_at))).one_or_none()


async def locked(conn: AsyncConnection, email_id: int):
    """The row of address ``email_id``, locked, or None."""
    e = tables.user_emails
    return (await conn.execute(listed().where(e.c.id == email_id).with_for_update(of=e))).one_or_none()


async def remove(conn: AsyncConnection, email_id: int) -> None:
    e = tables.user_emails
    await conn.execute(delete(e).where(e.c.id == email_id))


async def logins_of_keys(conn: AsyncConnection, keys: Iterable[str]) -> dict[str, str]:
    """Each of the lower-cased addresses ``keys`` a member listed, to that member's login."""
    e, users = tables.user_emails, tables.users
    found = await conn.execute(
        select(func.lower(e.c.email), users.c.login)
        .join_from(e, users, users.c.id == e.c.user_id)
        .where(func.lower(e.c.email).in_(keys))
    )
    return {key: login for key, login in found}
