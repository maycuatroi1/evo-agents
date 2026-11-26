"""The queries of messages sent from interactive answers (``chats.service.answers``)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, tables


async def answer_rows(conn: AsyncConnection, chat_id: int, output_id: int) -> list:
    """The messages of chat ``chat_id`` an agent wrote that carry interactive output ``output_id``, oldest first: rows
    (id, thread_root_id, author_profile_id, label)."""
    k, cm, o = tables.run_output_links, tables.chat_messages, tables.run_outputs
    query = (
        select(cm.c.id, cm.c.thread_root_id, cm.c.author_profile_id, cm.c.label)
        .join_from(k, cm, cm.c.id == k.c.chat_message_id)
        .join(o, o.c.id == k.c.output_id)
        .where(
            k.c.output_id == output_id,
            cm.c.chat_id == chat_id,
            cm.c.author_profile_id.is_not(None),
            o.c.interactive.is_(True),
        )
        .order_by(cm.c.id)
    )
    return list((await conn.execute(query)).all())


async def answer_agent(conn: AsyncConnection, chat_id: int, profile_id: int):
    """Agent ``profile_id``'s handle, when it was deleted and whether it is in chat ``chat_id``: a row (handle,
    deleted_at, member)."""
    ap, m = tables.agent_profiles, tables.chat_members
    member = (
        select(m.c.id)
        .where(m.c.chat_id == chat_id, m.c.profile_id == ap.c.id, m.c.removed_at.is_(None))
        .exists()
        .label("member")
    )
    return (await conn.execute(select(ap.c.handle, ap.c.deleted_at, member).where(ap.c.id == profile_id))).one()


async def sent_lately(conn: AsyncConnection, user_id: int) -> int:
    """How many messages from interactive answers person ``user_id`` sent in the last ``chats.ANSWER_WINDOW`` seconds."""
    cm = tables.chat_messages
    since = func.now() - timedelta(seconds=chats.ANSWER_WINDOW)
    query = select(func.count()).where(
        cm.c.author_user_id == user_id, cm.c.from_output_id.is_not(None), cm.c.created_at > since
    )
    return (await conn.execute(query)).scalar_one()
