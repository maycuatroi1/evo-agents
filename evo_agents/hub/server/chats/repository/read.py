"""The queries of group chat messages read whole (``chats.service.read``)."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.chats.repository.views import message_select


async def message_row(conn: AsyncConnection, message_id: int):
    """The row (``message_select``) of message ``message_id``; None for none."""
    cm = tables.chat_messages
    return (await conn.execute(message_select().where(cm.c.id == message_id))).one_or_none()


async def thread_rows(conn: AsyncConnection, chat_id: int, root: int, limit: int) -> list:
    """The first ``limit`` rows (``message_select``) of the thread of message ``root`` of chat ``chat_id``, by seq."""
    cm = tables.chat_messages
    query = message_select().where(cm.c.chat_id == chat_id, or_(cm.c.id == root, cm.c.thread_root_id == root))
    return list((await conn.execute(query.order_by(cm.c.seq).limit(limit))).all())


async def chat_title(conn: AsyncConnection, chat_id: int) -> str:
    c = tables.chats
    return (await conn.execute(select(c.c.title).where(c.c.id == chat_id))).scalar_one()


async def project_levels(conn: AsyncConnection, project: str) -> list[str]:
    """The ladder of levels of project ``project``; none for no such project."""
    found = select(tables.projects.c.levels).where(tables.projects.c.name == project)
    return list((await conn.execute(found)).scalar_one_or_none() or [])
