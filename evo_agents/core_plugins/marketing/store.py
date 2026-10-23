"""The plugin marketing's reads and writes of its own tables (SQLAlchemy Core), and the refusal every module raises.

A channel account is one row of marketing_channels; a post is one row of marketing_posts that the gate moves through
proposed, judging and then posted, draft, failed or withdrawn. The kill switch of a project is its row of
marketing_state; the deny-list is marketing_denylist. Rows come back as SQLAlchemy rows: columns by name.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server.projects import ProjectAccess

PLUGIN = "marketing"
MAX_LIST = 500
OPEN = ("proposed", "judging")  # the states of a post the gate has not decided


class MarketingError(Exception):
    """Why the plugin refuses a request, with the HTTP status a route answers; nothing was written."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# Who may do what


def reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise MarketingError(403, f"reading the marketing of project {access.name} needs a grant on it")


def writer(access: ProjectAccess, what: str) -> None:
    reader(access)
    if not has_role(access.role, "writer"):
        raise MarketingError(403, f"{what} in project {access.name} needs the writer role; you are {access.role}")


def admin(access: ProjectAccess, what: str) -> None:
    reader(access)
    if not has_role(access.role, "admin"):
        raise MarketingError(403, f"{what} in project {access.name} needs the admin role; you are {access.role}")


# Channels


async def channels(conn: AsyncConnection, project_id: int) -> list:
    c = tables.marketing_channels.c
    found = await conn.execute(select(tables.marketing_channels).where(c.project_id == project_id).order_by(c.id))
    return list(found)


async def channel(conn: AsyncConnection, project_id: int, channel_id: int, *, lock: bool = False):
    c = tables.marketing_channels
    query = select(c).where(c.c.project_id == project_id, c.c.id == channel_id)
    return (await conn.execute(query.with_for_update() if lock else query)).first()


async def add_channel(conn: AsyncConnection, project_id: int, **values) -> int | None:
    """The id of the account made, None when the project has this channel and account already."""
    t = tables.marketing_channels
    statement = (
        pg_insert(t)
        .values(project_id=project_id, **values)
        .on_conflict_do_nothing(constraint="marketing_channels_account_key")
        .returning(t.c.id)
    )
    return (await conn.execute(statement)).scalar()


async def update_channel(conn: AsyncConnection, channel_id: int, **values) -> None:
    t = tables.marketing_channels
    await conn.execute(update(t).where(t.c.id == channel_id).values(**values))


async def pause_channel(conn: AsyncConnection, channel_id: int, reason: str | None) -> bool:
    """Whether this call opened the breaker of the channel (it was closed)."""
    t = tables.marketing_channels
    done = await conn.execute(
        update(t)
        .where(t.c.id == channel_id, t.c.paused_at.is_(None))
        .values(paused_at=func.now(), paused_reason=reason)
        .returning(t.c.id)
    )
    return done.scalar() is not None


async def open_channels(conn: AsyncConnection, project_id: int, since: datetime) -> int:
    """How many channels of the project opened their breaker since ``since``."""
    t = tables.marketing_channels.c
    return await conn.scalar(
        select(func.count()).where(t.project_id == project_id, t.paused_at.is_not(None), t.paused_at >= since)
    )


# Posts


async def posts(conn: AsyncConnection, project_id: int, *, state: str | None = None, limit: int = MAX_LIST) -> list:
    t = tables.marketing_posts.c
    query = select(tables.marketing_posts).where(t.project_id == project_id)
    if state:
        query = query.where(t.state == state)
    return list(await conn.execute(query.order_by(t.id.desc()).limit(limit)))


async def post(conn: AsyncConnection, post_id: int, *, project_id: int | None = None, lock: bool = False):
    t = tables.marketing_posts
    query = select(t).where(t.c.id == post_id)
    if project_id is not None:
        query = query.where(t.c.project_id == project_id)
    return (await conn.execute(query.with_for_update() if lock else query)).first()


async def add_post(conn: AsyncConnection, **values) -> int:
    t = tables.marketing_posts
    return (await conn.execute(insert(t).values(**values).returning(t.c.id))).scalar_one()


async def save_post(conn: AsyncConnection, post_id: int, **values) -> None:
    t = tables.marketing_posts
    await conn.execute(update(t).where(t.c.id == post_id).values(**values))


async def open_posts(conn: AsyncConnection, project_id: int) -> list:
    t = tables.marketing_posts.c
    found = await conn.execute(
        select(tables.marketing_posts).where(t.project_id == project_id, t.state.in_(OPEN)).order_by(t.id)
    )
    return list(found)


async def posted_since(conn: AsyncConnection, channel_id: int, since: datetime) -> list:
    """The posts that went out on the channel since ``since`` (dry runs excluded), oldest first."""
    t = tables.marketing_posts.c
    found = await conn.execute(
        select(tables.marketing_posts)
        .where(t.channel_id == channel_id, t.state == "posted", t.dry_run.is_(False), t.posted_at >= since)
        .order_by(t.posted_at)
    )
    return list(found)


async def last_posts(conn: AsyncConnection, channel_id: int, states: tuple[str, ...], limit: int) -> list:
    """The newest ``limit`` posts of the channel in one of ``states``, newest first."""
    t = tables.marketing_posts.c
    found = await conn.execute(
        select(tables.marketing_posts)
        .where(t.channel_id == channel_id, t.state.in_(states))
        .order_by(t.id.desc())
        .limit(limit)
    )
    return list(found)


# The deny-list and the kill switch


async def denylist(conn: AsyncConnection, project_id: int) -> list:
    t = tables.marketing_denylist.c
    found = await conn.execute(select(tables.marketing_denylist).where(t.project_id == project_id).order_by(t.id))
    return list(found)


async def add_deny(conn: AsyncConnection, project_id: int, kind: str, value: str, added_by: int | None) -> bool:
    t = tables.marketing_denylist
    statement = (
        pg_insert(t)
        .values(project_id=project_id, kind=kind, value=value, added_by=added_by)
        .on_conflict_do_nothing(constraint="marketing_denylist_value_key")
        .returning(t.c.id)
    )
    return (await conn.execute(statement)).scalar() is not None


async def remove_deny(conn: AsyncConnection, project_id: int, deny_id: int) -> bool:
    t = tables.marketing_denylist.c
    done = await conn.execute(
        delete(tables.marketing_denylist).where(t.project_id == project_id, t.id == deny_id).returning(t.id)
    )
    return done.scalar() is not None


async def killed(conn: AsyncConnection, project_id: int):
    """The row of the kill switch when it is set, else None."""
    t = tables.marketing_state
    found = (await conn.execute(select(t).where(t.c.project_id == project_id, t.c.killed.is_(True)))).first()
    return found


async def set_killed(conn: AsyncConnection, project_id: int, on: bool, reason: str | None, by: int | None) -> bool:
    """Set or clear the kill switch; whether the state changed."""
    t = tables.marketing_state
    before = await killed(conn, project_id) is not None
    values = {"killed": on, "reason": reason if on else None, "killed_by": by if on else None}
    values["killed_at"] = func.now() if on else None
    statement = pg_insert(t).values(project_id=project_id, **values)
    await conn.execute(statement.on_conflict_do_update(constraint="marketing_state_pkey", set_=values))
    return before != on


async def project_admins(conn: AsyncConnection, project_id: int) -> list[int]:
    g = tables.grants.c
    found = await conn.execute(select(g.user_id).where(g.project_id == project_id, g.role == "admin"))
    return [row.user_id for row in found]
