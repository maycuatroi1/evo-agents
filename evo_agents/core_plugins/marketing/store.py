"""The plugin marketing's reads and writes of its own tables (SQLAlchemy Core), and the refusal every module raises.

A channel account is one row of marketing_channels; a post is one row of marketing_posts that the gate moves through
proposed, judging and then posted, draft, failed or withdrawn. The kill switch of a project is its row of
marketing_state; the deny-list is marketing_denylist. A post for a browser channel goes on to queued and posting
(``browser``). Rows come back as SQLAlchemy rows: columns by name.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.marketing import tables
from evo_agents.hub.server import core_plugin_api as hub

PLUGIN = "marketing"
MAX_LIST = 500
OPEN = ("proposed", "judging")  # the states of a post the gate has not decided


def handle() -> hub.Plugin:
    """The plugin's handle, which every call of the API takes."""
    return hub.plugin(__name__)


def post_columns() -> list:
    """The columns of marketing_posts a read takes: all but the screenshot, which only its own route reads."""
    return [column for column in tables.marketing_posts.c if column.name != "screenshot"]


class MarketingError(Exception):
    """Why the plugin refuses a request, with the HTTP status a route answers; nothing was written."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# Who may do what


def reader(access: hub.ProjectRef) -> None:
    if access.max_level is None:  # a hub admin without a grant: the API passes them as admin, marketing does not
        raise MarketingError(403, f"reading the marketing of project {access.name} needs a grant on it")


def writer(access: hub.ProjectRef, what: str) -> None:
    reader(access)
    if not hub.has_role(access.role, "writer"):
        raise MarketingError(403, f"{what} in project {access.name} needs the writer role; you are {access.role}")


def admin(access: hub.ProjectRef, what: str) -> None:
    reader(access)
    if not hub.has_role(access.role, "admin"):
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
    query = select(*post_columns()).where(t.project_id == project_id)
    if state:
        query = query.where(t.state == state)
    return list(await conn.execute(query.order_by(t.id.desc()).limit(limit)))


async def post(conn: AsyncConnection, post_id: int, *, project_id: int | None = None, lock: bool = False):
    t = tables.marketing_posts
    query = select(*post_columns()).where(t.c.id == post_id)
    if project_id is not None:
        query = query.where(t.c.project_id == project_id)
    return (await conn.execute(query.with_for_update() if lock else query)).first()


async def add_post(conn: AsyncConnection, **values) -> int:
    t = tables.marketing_posts
    return (await conn.execute(insert(t).values(**values).returning(t.c.id))).scalar_one()


async def save_post(conn: AsyncConnection, post_id: int, **values) -> None:
    t = tables.marketing_posts
    await conn.execute(update(t).where(t.c.id == post_id).values(**values))


async def screenshot(conn: AsyncConnection, post_id: int, project_id: int) -> bytes | None:
    """The PNG a browser script took for the post, None when there is none."""
    t = tables.marketing_posts.c
    return await conn.scalar(select(t.screenshot).where(t.id == post_id, t.project_id == project_id))


async def in_flight(conn: AsyncConnection, channel_id: int) -> bool:
    """Whether a post of the channel waits for a worker's script or is being posted by one."""
    t = tables.marketing_posts.c
    found = await conn.scalar(
        select(t.id).where(t.channel_id == channel_id, t.state.in_(("queued", "posting"))).limit(1)
    )
    return found is not None


async def open_posts(conn: AsyncConnection, project_id: int) -> list:
    """The posts the gate has not decided, but a scheduled one the Loop has not released yet (``loop``)."""
    t = tables.marketing_posts.c
    found = await conn.execute(
        select(*post_columns())
        .where(
            t.project_id == project_id,
            t.state.in_(OPEN),
            or_(t.scheduled_at.is_(None), t.released_at.is_not(None)),
        )
        .order_by(t.id)
    )
    return list(found)


async def due_posts(conn: AsyncConnection, project_id: int, now: datetime, limit: int) -> list:
    """The scheduled posts whose time came that the Loop has not released, oldest time first."""
    t = tables.marketing_posts.c
    found = await conn.execute(
        select(t.id, t.claim_id)
        .where(
            t.project_id == project_id,
            t.state == "proposed",
            t.scheduled_at <= now,
            t.released_at.is_(None),
        )
        .order_by(t.scheduled_at, t.id)
        .limit(limit)
    )
    return list(found)


async def release(conn: AsyncConnection, post_id: int, now: datetime) -> None:
    """Let a scheduled post through to the gate: the job ``advance`` decides it next."""
    t = tables.marketing_posts
    await conn.execute(update(t).where(t.c.id == post_id, t.c.released_at.is_(None)).values(released_at=now))


async def gone_out(conn: AsyncConnection, project_id: int, since: datetime, limit: int) -> list:
    """The posts that went out since ``since`` (dry runs excluded), oldest first: each is measured."""
    t = tables.marketing_posts.c
    found = await conn.execute(
        select(t.id, t.claim_id)
        .where(t.project_id == project_id, t.state == "posted", t.dry_run.is_(False), t.posted_at >= since)
        .order_by(t.posted_at, t.id)
        .limit(limit)
    )
    return list(found)


async def kept_numbers(conn: AsyncConnection, post_id: int) -> dict[str, dict[str, float]]:
    """span -> metric -> value of the numbers kept for a post."""
    m = tables.marketing_metrics.c
    found: dict[str, dict[str, float]] = {}
    for row in await conn.execute(select(m.span, m.metric, m.value).where(m.post_id == post_id)):
        found.setdefault(row.span, {})[row.metric] = float(row.value)
    return found


# Replies to the posts of a project account


async def add_reply(conn: AsyncConnection, project_id: int, post_id: int, **values) -> bool:
    """Whether the reply was new; the same reply of a post is kept once."""
    t = tables.marketing_replies
    statement = (
        pg_insert(t)
        .values(project_id=project_id, post_id=post_id, **values)
        .on_conflict_do_nothing(constraint="marketing_replies_once_key")
        .returning(t.c.id)
    )
    return (await conn.execute(statement)).scalar() is not None


async def new_replies(conn: AsyncConnection, project_id: int, since: datetime, limit: int) -> list:
    """The replies seen since ``since`` under posts of the project's project accounts, oldest first, with the post's
    channel and its reply target."""
    r, p, c = tables.marketing_replies, tables.marketing_posts, tables.marketing_channels
    found = await conn.execute(
        select(
            r.c.id,
            r.c.post_id,
            r.c.external_id,
            r.c.author,
            r.c.url,
            r.c.body,
            p.c.channel_id,
            p.c.channel,
            p.c.claim_id,
            p.c.posted_url,
        )
        .join_from(r, p, p.c.id == r.c.post_id)
        .join(c, c.c.id == p.c.channel_id)
        .where(r.c.project_id == project_id, r.c.seen_at >= since, c.c.account_kind == "project")
        .order_by(r.c.id)
        .limit(limit)
    )
    return list(found)


async def posted_since(conn: AsyncConnection, channel_id: int, since: datetime) -> list:
    """The posts that went out on the channel since ``since`` (dry runs excluded), oldest first."""
    t = tables.marketing_posts.c
    found = await conn.execute(
        select(*post_columns())
        .where(t.channel_id == channel_id, t.state == "posted", t.dry_run.is_(False), t.posted_at >= since)
        .order_by(t.posted_at)
    )
    return list(found)


async def last_posts(conn: AsyncConnection, channel_id: int, states: tuple[str, ...], limit: int) -> list:
    """The newest ``limit`` posts of the channel in one of ``states``, newest first."""
    t = tables.marketing_posts.c
    found = await conn.execute(
        select(*post_columns())
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
