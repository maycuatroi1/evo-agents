"""The queries of the route of a profile's chat runs (``chats.service.route``)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import enabled_query


async def agent_types_of(conn: AsyncConnection, profile_id: int) -> list:
    """The agent types the projects enabled profile ``profile_id`` for, while the type is on and its plugin enabled and
    on in the project: rows (manifest, agent_type, run_as, run_as_login)."""
    t, u = tables.project_agent_types, tables.users
    query = (
        enabled_query()
        .add_columns(t.c.name.label("agent_type"), t.c.run_as, u.c.login.label("run_as_login"))
        .join(t, (t.c.plugin_id == tables.plugins.c.id) & (t.c.project_id == tables.projects.c.id))
        .join(u, u.c.id == t.c.run_as)
        .where(t.c.profile_id == profile_id, t.c.disabled_at.is_(None))
    )
    return list((await conn.execute(query)).all())


async def run_origin(conn: AsyncConnection, run_id: int):
    """Run ``run_id``'s kind and dispatcher, with the author and run of the message that started it, or None."""
    r, m = tables.runs, tables.chat_messages
    query = (
        select(r.c.kind, r.c.dispatched_by, m.c.author_user_id, m.c.run_id)
        .select_from(r.outerjoin(m, m.c.id == r.c.chat_message_id))
        .where(r.c.id == run_id)
    )
    return (await conn.execute(query)).one_or_none()
