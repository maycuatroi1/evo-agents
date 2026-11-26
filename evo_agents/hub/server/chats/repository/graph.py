"""The query of the messages of a project's knowledge graph (``chats.service.graph``)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


def _messages(project: str):
    """Every message whose label holds a part of ``project``, with its chat's title and its author, in order."""
    cm, c, u, ap = tables.chat_messages, tables.chats, tables.users, tables.agent_profiles
    joined = (
        cm.join(c, c.c.id == cm.c.chat_id)
        .outerjoin(u, u.c.id == cm.c.author_user_id)
        .outerjoin(ap, ap.c.id == cm.c.author_profile_id)
    )
    return (
        select(
            cm.c.id,
            cm.c.chat_id,
            cm.c.seq,
            cm.c.thread_root_id,
            cm.c.run_id,
            cm.c.body,
            cm.c.label,
            cm.c.created_at,
            c.c.title,
            u.c.login,
            ap.c.handle,
            cm.c.guest_name,
        )
        .select_from(joined)
        .where(cm.c.label.contains([{"projects": [project]}]))
        .order_by(cm.c.id)
    )


async def messages_of(conn: AsyncConnection, project: str) -> list:
    """The rows of ``_messages``."""
    return list(await conn.execute(_messages(project)))
