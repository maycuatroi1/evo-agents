"""The SQL of signing in and of who the caller is (``evo_agents.hub.server.auth``): the users row of a GitHub account,
the grants it holds and the token a request came with."""

from __future__ import annotations

from sqlalchemy import Text, cast, delete, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def move_aside(conn: AsyncConnection, user_id: int) -> None:
    """Give up the login of a row whose GitHub account was renamed since: ``_`` cannot occur in a GitHub login, so
    the new name collides with nobody, and the account gets its real login back when it signs in again."""
    users = tables.users
    suffix = cast(func.coalesce(users.c.github_id, users.c.id), Text)
    await conn.execute(
        update(users).values(login=func.left(users.c.login, 80, type_=Text) + "_" + suffix).where(users.c.id == user_id)
    )


async def merge_grants(conn: AsyncConnection, into: int, holder_id: int) -> None:
    """Copy the grants of row ``holder_id`` to row ``into``, keeping those ``into`` holds on the same project."""
    grants = tables.grants
    moved = select(
        literal(into, grants.c.user_id.type),
        grants.c.project_id,
        grants.c.role,
        grants.c.max_level,
        grants.c.granted_by,
        grants.c.granted_at,
    ).where(grants.c.user_id == holder_id)
    columns = ["user_id", "project_id", "role", "max_level", "granted_by", "granted_at"]
    await conn.execute(
        pg_insert(grants)
        .from_select(columns, moved)
        .on_conflict_do_nothing(index_elements=[grants.c.user_id, grants.c.project_id])
    )


async def own_row(conn: AsyncConnection, github_id: int):
    """(id,) of the users row of GitHub account ``github_id``, locked, or None."""
    users = tables.users
    return (await conn.execute(select(users.c.id).where(users.c.github_id == github_id).with_for_update())).first()


async def login_holder(conn: AsyncConnection, login: str):
    """(id, github_id) of the users row that holds ``login`` in any case, locked, or None."""
    users = tables.users
    return (
        await conn.execute(
            select(users.c.id, users.c.github_id)
            .where(func.lower(users.c.login) == func.lower(login))
            .with_for_update()
        )
    ).first()


async def claim(conn: AsyncConnection, user_id: int, github_id: int, login: str) -> None:
    """Tie row ``user_id``, granted before its first sign-in, to GitHub account ``github_id``."""
    users = tables.users
    await conn.execute(
        update(users).values(github_id=github_id, login=login, last_seen_at=func.now()).where(users.c.id == user_id)
    )


async def delete_user(conn: AsyncConnection, user_id: int) -> None:
    users = tables.users
    await conn.execute(delete(users).where(users.c.id == user_id))


async def seen(conn: AsyncConnection, user_id: int, login: str) -> None:
    """Bring row ``user_id`` up to date with its GitHub login, seen now."""
    users = tables.users
    await conn.execute(update(users).values(login=login, last_seen_at=func.now()).where(users.c.id == user_id))


async def create(conn: AsyncConnection, github_id: int, login: str) -> int | None:
    """The id of a new users row for GitHub account ``github_id``, or None when a row of that login or account came
    first."""
    users = tables.users
    created = await conn.execute(
        pg_insert(users)
        .values(login=login, github_id=github_id, last_seen_at=func.now())
        .on_conflict_do_nothing()
        .returning(users.c.id)
    )
    return created.scalar()


async def token_info(conn: AsyncConnection, token_id: int):
    """The row of token ``token_id`` as TokenInfo reads it."""
    tokens = tables.tokens
    return (
        await conn.execute(
            select(
                tokens.c.id,
                tokens.c.kind,
                tokens.c.host,
                tokens.c.created_at,
                tokens.c.last_used_at,
                tokens.c.expires_at,
            ).where(tokens.c.id == token_id)
        )
    ).one()


async def grants_of(conn: AsyncConnection, user_id: int):
    """(project, role, max_level) of each grant ``user_id`` holds, by project."""
    grants, projects = tables.grants, tables.projects
    held = await conn.execute(
        select(projects.c.name.label("project"), grants.c.role, grants.c.max_level)
        .join_from(grants, projects, projects.c.id == grants.c.project_id)
        .where(grants.c.user_id == user_id)
        .order_by(projects.c.name)
    )
    return held.all()
