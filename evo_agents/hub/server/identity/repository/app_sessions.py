"""The SQL of app sessions (``evo_agents.hub.server.app_sessions``): the apps, the members their subjects are, and the
sessions an app opened."""

from __future__ import annotations

from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import ColumnElement

from evo_agents.hub import tables


async def live_app(conn: AsyncConnection, name: str):
    """(id, sealed_secret) of the unrevoked app ``name``, or None."""
    apps = tables.auth_apps
    return (
        await conn.execute(
            select(apps.c.id, apps.c.sealed_secret).where(apps.c.name == name, apps.c.revoked_at.is_(None))
        )
    ).first()


async def lock_subject(conn: AsyncConnection, app_id: int, subject: str) -> None:
    """Hold ``subject`` of app ``app_id`` until the transaction ends, so two first sign-ins make one member."""
    await conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"{app_id}:{subject}", 0))))


async def member_of(conn: AsyncConnection, app_id: int, subject: str):
    """(id, login) of the member ``subject`` of app ``app_id`` is, or None."""
    identities, users = tables.app_identities, tables.users
    return (
        await conn.execute(
            select(users.c.id, users.c.login)
            .join_from(identities, users, users.c.id == identities.c.user_id)
            .where(identities.c.app_id == app_id, identities.c.subject == subject)
        )
    ).first()


async def create_user(conn: AsyncConnection, login: str) -> int | None:
    """The id of a new users row of ``login`` with no GitHub account, or None when a row holds that login."""
    users = tables.users
    made = await conn.execute(pg_insert(users).values(login=login).on_conflict_do_nothing().returning(users.c.id))
    return made.scalar()


async def claimable(conn: AsyncConnection, login: str):
    """(id, login) of the row of ``login`` an admin granted before a first sign-in, tied to no GitHub account and no
    app, locked; or None."""
    identities, users = tables.app_identities, tables.users
    tied = exists().where(identities.c.user_id == users.c.id)
    held = await conn.execute(
        select(users.c.id, users.c.login)
        .where(func.lower(users.c.login) == login.lower(), users.c.github_id.is_(None), ~tied)
        .with_for_update()
    )
    return held.first()


async def tie(conn: AsyncConnection, app_id: int, subject: str, user_id: int) -> None:
    await conn.execute(pg_insert(tables.app_identities).values(app_id=app_id, subject=subject, user_id=user_id))


async def seen(conn: AsyncConnection, user_id: int) -> None:
    users = tables.users
    await conn.execute(update(users).values(last_seen_at=func.now()).where(users.c.id == user_id))


async def admin_as(conn: AsyncConnection, user_id: int, admin_login: ColumnElement) -> str | None:
    """``admin_login`` (the login the admins of the hub's config name a member by) of ``user_id``, or None."""
    users = tables.users
    return (await conn.execute(select(admin_login).where(users.c.id == user_id))).scalar_one_or_none()


async def apps(conn: AsyncConnection):
    """Every app registered, revoked ones too, by name, with who made it and the people it signed in."""
    apps, users, identities = tables.auth_apps, tables.users, tables.app_identities
    members = select(func.count()).where(identities.c.app_id == apps.c.id).scalar_subquery()
    query = (
        select(
            apps.c.name,
            users.c.login.label("created_by"),
            apps.c.created_at,
            apps.c.revoked_at,
            members.label("members"),
        )
        .join_from(apps, users, users.c.id == apps.c.created_by)
        .order_by(apps.c.name)
    )
    return (await conn.execute(query)).all()


async def create_app(conn: AsyncConnection, name: str, sealed: dict, created_by: int):
    """When the new app ``name`` was made, or None when an app holds that name."""
    apps = tables.auth_apps
    made = await conn.execute(
        pg_insert(apps)
        .values(name=name, sealed_secret=sealed, created_by=created_by)
        .on_conflict_do_nothing()
        .returning(apps.c.created_at)
    )
    return made.scalar()


async def revoke_app(conn: AsyncConnection, name: str) -> bool:
    """Revoke the live app ``name``; False without one."""
    apps = tables.auth_apps
    revoked = await conn.execute(
        update(apps)
        .values(revoked_at=func.now())
        .where(apps.c.name == name, apps.c.revoked_at.is_(None))
        .returning(apps.c.id)
    )
    return revoked.first() is not None


async def revoke_sessions(conn: AsyncConnection, kind: str, host: str) -> None:
    """Revoke every live session of tokens kind ``kind`` and host ``host``."""
    tokens = tables.tokens
    await conn.execute(
        update(tokens)
        .values(revoked_at=func.now())
        .where(tokens.c.kind == kind, tokens.c.host == host, tokens.c.revoked_at.is_(None))
    )
