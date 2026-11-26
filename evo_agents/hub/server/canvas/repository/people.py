"""The SQL the plugin canvas reads of the hub's own tables: a project by name, a member by login or id, the grants of
a project, a run, and a canvas's slug by id.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def project_id_of(conn: AsyncConnection, name: str) -> int | None:
    """The id of project ``name``; None when there is none."""
    p = tables.projects
    found = await conn.scalar(select(p.c.id).where(p.c.name == name))
    return None if found is None else int(found)


async def user_id_of(conn: AsyncConnection, login: str) -> int | None:
    """The id of the member ``login``; None when there is none."""
    u = tables.users
    found = (await conn.execute(select(u.c.id).where(u.c.login == login))).scalar_one_or_none()
    return None if found is None else int(found)


async def login_of(conn: AsyncConnection, user_id: int) -> str | None:
    u = tables.users
    found = await conn.scalar(select(u.c.login).where(u.c.id == user_id))
    return None if found is None else str(found)


async def grants(conn: AsyncConnection, project_id: int) -> list[tuple[int, str, Any]]:
    """(member id, role, max level) of each grant on project ``project_id``, by member."""
    g = tables.grants
    query = select(g.c.user_id, g.c.role, g.c.max_level).where(g.c.project_id == project_id).order_by(g.c.user_id)
    return [(row.user_id, row.role, row.max_level) for row in await conn.execute(query)]


async def run_of(conn: AsyncConnection, run_id: int) -> Any:
    """The kind, profile, dispatcher, runtime, model and chat message of run ``run_id``."""
    r = tables.runs
    columns = (r.c.kind, r.c.profile_id, r.c.dispatched_by, r.c.runtime, r.c.model, r.c.chat_message_id)
    return (await conn.execute(select(*columns).where(r.c.id == run_id))).one()


async def canvas_slug(conn: AsyncConnection, canvas_id: int) -> str | None:
    c = tables.canvas.canvases
    found = await conn.scalar(select(c.c.slug).where(c.c.id == canvas_id))
    return None if found is None else str(found)
