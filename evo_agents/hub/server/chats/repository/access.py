"""The queries of a chat's access and label (``chats.service.access``): the projects a label names, the level a chat
set, the shares of its parts, the grants of its people, and one person's membership of a chat.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import and_, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import access, tables
from evo_agents.hub.db import one_of
from evo_agents.hub.server.chats.models.access import Grants, LabelProject


def _channels():
    """The clearances of a project's sinks of kind channel, as one JSONB object by id; {} without one."""
    p, cs = tables.projects, tables.project_sinks.alias("channel_sinks")
    found = func.jsonb_object_agg(cs.c.sink_id, cs.c.clearance, type_=JSONB)
    query = select(func.coalesce(found, literal({}, JSONB), type_=JSONB))
    return query.where(cs.c.project_id == p.c.id, cs.c.kind == access.CHANNEL_KIND).scalar_subquery()


def _projects():
    p, ps = tables.projects, tables.project_sinks
    hub_sink = and_(ps.c.project_id == p.c.id, ps.c.kind == access.HUB_KIND)
    return select(
        p.c.id,
        p.c.name,
        p.c.levels,
        p.c.locations,
        p.c.default_label,
        ps.c.clearance.label("clearance"),
        _channels().label("channels"),
    ).select_from(p.outerjoin(ps, hub_sink))


def _project(row) -> LabelProject:
    return LabelProject(
        id=row.id,
        name=row.name,
        levels=list(row.levels),
        locations=list(row.locations),
        default_label=dict(row.default_label),
        clearance=row.clearance,
        channels=dict(row.channels or {}),
    )


async def projects_named(conn: AsyncConnection, names: Iterable[str]) -> dict[str, LabelProject]:
    """The projects named ``names``, by name; a name with no project is left out."""
    wanted = sorted(set(names))
    if not wanted:
        return {}
    rows = await conn.execute(_projects().where(one_of(tables.projects.c.name, wanted)))
    return {row.name: _project(row) for row in rows}


async def projects_by_id(conn: AsyncConnection, ids: Iterable[int]) -> list[LabelProject]:
    wanted = sorted(set(ids))
    if not wanted:
        return []
    rows = await conn.execute(_projects().where(one_of(tables.projects.c.id, wanted)))
    return [_project(row) for row in rows]


async def label_override(conn: AsyncConnection, chat_id: int) -> dict | None:
    """The level set for chat ``chat_id`` and the projects it lowers (chats.label_override); None when unset."""
    c = tables.chats
    return (await conn.execute(select(c.c.label_override).where(c.c.id == chat_id))).scalar_one_or_none()


async def label_shares(conn: AsyncConnection, chat_ids: Iterable[int]) -> list:
    """The shares of chats ``chat_ids`` (chat_label_shares) with the role and max level the writer who shared each holds
    on its project now, None for none: rows (chat_id, project_id, user_id, role, max_level)."""
    wanted = sorted(set(chat_ids))
    if not wanted:
        return []
    s, g = tables.chat_label_shares, tables.grants
    held = and_(g.c.user_id == s.c.shared_by, g.c.project_id == s.c.project_id)
    query = select(s.c.chat_id, s.c.project_id, s.c.user_id, g.c.role, g.c.max_level).select_from(s.outerjoin(g, held))
    return list(await conn.execute(query.where(one_of(s.c.chat_id, wanted))))


async def grants_of(conn: AsyncConnection, user_ids: Iterable[int], project_ids: Iterable[int]) -> Grants:
    users, projects = sorted(set(user_ids)), sorted(set(project_ids))
    if not users or not projects:
        return {}
    g = tables.grants
    query = select(g.c.user_id, g.c.project_id, g.c.role, g.c.max_level).where(
        one_of(g.c.user_id, users), one_of(g.c.project_id, projects)
    )
    return {(row.user_id, row.project_id): (row.role, row.max_level) for row in await conn.execute(query)}


def _membership(chat_id: int, user_id: int):
    c, m, u = tables.chats, tables.chat_members, tables.users
    member = and_(m.c.chat_id == c.c.id, m.c.user_id == user_id, m.c.removed_at.is_(None))
    return (
        select(
            c.c.id,
            c.c.title,
            c.c.created_by,
            u.c.login.label("creator"),
            c.c.created_at,
            c.c.event_seq,
            m.c.id.label("member_id"),
            m.c.last_read_seq,
            c.c.kind,
            c.c.label_override,
        )
        .select_from(c.join(m, member).join(u, u.c.id == c.c.created_by))
        .where(c.c.id == chat_id)
    )


async def membership(conn: AsyncConnection, chat_id: int, user_id: int, *, lock: bool = False):
    """The row of chat ``chat_id`` with person ``user_id``'s membership of it, None when they are not in it; with
    ``lock`` the chat's row is locked, as a change of its members needs."""
    query = _membership(chat_id, user_id)
    if lock:
        query = query.with_for_update(of=tables.chats)
    return (await conn.execute(query)).one_or_none()


async def agent_project_ids(conn: AsyncConnection, chat_id: int) -> list[int]:
    """The project of each agent in chat ``chat_id`` now, one per agent."""
    m, ap = tables.chat_members, tables.agent_profiles
    query = select(ap.c.project_id).join_from(m, ap, ap.c.id == m.c.profile_id)
    query = query.where(m.c.chat_id == chat_id, m.c.removed_at.is_(None))
    return list((await conn.execute(query)).scalars().all())


async def member_chats(conn: AsyncConnection, user_id: int) -> list[int]:
    """The chats person ``user_id`` is in now, by id."""
    m = tables.chat_members
    query = select(m.c.chat_id).where(m.c.user_id == user_id, m.c.removed_at.is_(None))
    return sorted((await conn.execute(query)).scalars().all())


async def chat_agent_projects(conn: AsyncConnection, chat_ids: list[int]) -> list:
    """Rows (chat_id, project_id): the project of each agent in each chat of ``chat_ids`` now."""
    m, ap = tables.chat_members, tables.agent_profiles
    query = select(m.c.chat_id, ap.c.project_id).join_from(m, ap, ap.c.id == m.c.profile_id)
    query = query.where(one_of(m.c.chat_id, chat_ids), m.c.removed_at.is_(None))
    return list((await conn.execute(query)).all())


async def label_overrides(conn: AsyncConnection, chat_ids: list[int]) -> dict[int, dict]:
    """The level set for each chat of ``chat_ids`` that has one (chats.label_override), by chat id."""
    c = tables.chats
    query = select(c.c.id, c.c.label_override).where(one_of(c.c.id, chat_ids), c.c.label_override.is_not(None))
    return {row.id: row.label_override for row in await conn.execute(query)}
