"""The SQL of invite links (``evo_agents.hub.server.invites``): the links with their status, a link locked, and what
accepting a link and spending its right write."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import case, func, insert, select, update
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import Executable

from evo_agents.hub import tables
from evo_agents.hub.server.identity.models.invites import ACCEPTED, EXPIRED, OPEN, REGISTER, REVOKED, InviteRow
from evo_agents.hub.server.paging import Paging


def rows():
    """Every link as InviteRow reads it, its status computed at the time of the query."""
    invites, projects = tables.invites, tables.projects
    creator, accepter = tables.users.alias("creator"), tables.users.alias("accepter")
    status = case(
        (invites.c.revoked_at.is_not(None), REVOKED),
        (invites.c.accepted_at.is_not(None), ACCEPTED),
        (invites.c.expires_at <= func.now(), EXPIRED),
        else_=OPEN,
    )
    return select(
        invites.c.id,
        invites.c.kind,
        projects.c.name.label("project"),
        invites.c.role,
        invites.c.max_level,
        invites.c.for_login,
        creator.c.login.label("created_by"),
        invites.c.created_at,
        invites.c.expires_at,
        status.label("status"),
        accepter.c.login.label("accepted_by"),
        invites.c.accepted_at,
        invites.c.used_at,
    ).select_from(
        invites.join(creator, creator.c.id == invites.c.created_by)
        .outerjoin(projects, projects.c.id == invites.c.project_id)
        .outerjoin(accepter, accepter.c.id == invites.c.accepted_by)
    )


ROWS = rows()


async def one(conn: AsyncConnection, invite_id: int) -> InviteRow:
    row = (await conn.execute(ROWS.where(tables.invites.c.id == invite_id))).one()
    return InviteRow(**row._asdict())


async def page_of(conn: AsyncConnection, page: Paging):
    """The links on ``page``, newest first."""
    return await page.rows(conn, ROWS.order_by(tables.invites.c.id.desc()))


async def by_token_hash(conn: AsyncConnection, token_hash: str):
    """The link of token hash ``token_hash`` as InviteRow reads it, or None."""
    return (await conn.execute(ROWS.where(tables.invites.c.token_hash == token_hash))).first()


async def insert_invite(
    conn: AsyncConnection,
    *,
    kind: str,
    project_id: int | None,
    role: str | None,
    max_level: str | None,
    for_login: str | None,
    token_hash: str,
    created_by: int,
    days: int,
) -> int:
    """The id of a new link that lasts ``days``."""
    invites = tables.invites
    made = await conn.execute(
        insert(invites)
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
        .returning(invites.c.id)
    )
    return made.scalar_one()


async def locked(conn: AsyncConnection, *where) -> Row | None:
    """The link ``where`` finds, its row locked until the transaction ends, with the database's time as ``now``."""
    invites = tables.invites
    found = await conn.execute(select(invites, func.now().label("now")).where(*where).with_for_update(of=invites))
    return found.first()


async def locked_by_id(conn: AsyncConnection, invite_id: int) -> Row | None:
    return await locked(conn, tables.invites.c.id == invite_id)


async def locked_by_token_hash(conn: AsyncConnection, token_hash: str) -> Row | None:
    return await locked(conn, tables.invites.c.token_hash == token_hash)


async def set_revoked(conn: AsyncConnection, invite_id: int, by: int) -> None:
    invites = tables.invites
    await conn.execute(update(invites).where(invites.c.id == invite_id).values(revoked_at=func.now(), revoked_by=by))


async def set_accepted(conn: AsyncConnection, invite_id: int, by: int) -> None:
    invites = tables.invites
    await conn.execute(update(invites).where(invites.c.id == invite_id).values(accepted_at=func.now(), accepted_by=by))


async def project_name(conn: AsyncConnection, project_id: int) -> str:
    projects = tables.projects
    return (await conn.execute(select(projects.c.name).where(projects.c.id == project_id))).scalar_one()


async def held_grant(conn: AsyncConnection, user_id: int, project_id: int):
    """(role, max_level) of the grant ``user_id`` holds on ``project_id``, locked, or None."""
    grants = tables.grants
    return (
        await conn.execute(
            select(grants.c.role, grants.c.max_level)
            .where(grants.c.user_id == user_id, grants.c.project_id == project_id)
            .with_for_update()
        )
    ).first()


async def write(conn: AsyncConnection, statement: Executable) -> None:
    """Run a write the service built elsewhere (a grant as a hub admin's gives it)."""
    await conn.execute(statement)


async def register_right(conn: AsyncConnection, user_id: int) -> Row | None:
    """(id, created_by) of the oldest unused, unrevoked right to register a project that ``user_id`` accepted, its
    row locked; None without one."""
    invites = tables.invites
    found = await conn.execute(
        select(invites.c.id, invites.c.created_by)
        .where(
            invites.c.kind == REGISTER,
            invites.c.accepted_by == user_id,
            invites.c.used_at.is_(None),
            invites.c.revoked_at.is_(None),
        )
        .order_by(invites.c.id)
        .limit(1)
        .with_for_update()
    )
    return found.first()


async def set_used(conn: AsyncConnection, invite_id: int, project_id: int) -> None:
    invites = tables.invites
    await conn.execute(
        update(invites).where(invites.c.id == invite_id).values(used_at=func.now(), project_id=project_id)
    )
