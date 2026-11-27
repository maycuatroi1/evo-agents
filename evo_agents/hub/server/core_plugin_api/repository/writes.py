"""The rows the writes of a core plugin read and make: the ingest keys named after it, a project's first admin, a
join link, and the members of a project among some users."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def ingest_keys(conn: AsyncConnection, project_id: int, name: str):
    """The ingest keys of project ``project_id`` named ``name``: rows of id, max_level and revoked_at."""
    k = tables.signal_ingest_keys
    query = select(k.c.id, k.c.max_level, k.c.revoked_at).where(k.c.project_id == project_id, k.c.name == name)
    return (await conn.execute(query)).all()


async def first_admin(conn: AsyncConnection, project_id: int) -> int | None:
    """The admin of project ``project_id`` with the lowest user id, None without one."""
    g = tables.grants
    return await conn.scalar(
        select(g.c.user_id).where(g.c.project_id == project_id, g.c.role == "admin").order_by(g.c.user_id).limit(1)
    )


async def add_ingest_key(
    conn: AsyncConnection, project_id: int, name: str, max_level: str | None, token_hash: str, owner: int
):
    """Make the ingest key ``name`` of project ``project_id``: its row of id, max_level and revoked_at."""
    k = tables.signal_ingest_keys
    made = await conn.execute(
        insert(k)
        .values(project_id=project_id, name=name, max_level=max_level, token_hash=token_hash, created_by=owner)
        .returning(k.c.id, k.c.max_level, k.c.revoked_at)
    )
    return made.one()


async def add_join_invite(
    conn: AsyncConnection,
    *,
    kind: str,
    project_id: int,
    role: str,
    max_level: str,
    for_login: str | None,
    token_hash: str,
    created_by: int,
    days: int,
):
    """Make a join link valid ``days`` from now: its row of id and expires_at."""
    i = tables.invites
    made = await conn.execute(
        insert(i)
        .values(
            kind=kind,
            project_id=project_id,
            role=role,
            max_level=max_level,
            for_login=for_login,
            token_hash=token_hash,
            created_by=created_by,
            expires_at=func.now() + timedelta(days=days),
        )
        .returning(i.c.id, i.c.expires_at)
    )
    return made.one()


async def granted_users(conn: AsyncConnection, project_id: int, users: Sequence[int]) -> set[int]:
    """The users among ``users`` who hold a grant on project ``project_id``."""
    g = tables.grants
    query = select(g.c.user_id).where(g.c.project_id == project_id, g.c.user_id.in_(list(users)))
    return set((await conn.execute(query)).scalars())
