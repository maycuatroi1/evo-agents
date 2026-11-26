"""The queries of the image of a group chat (``chats.service.images``)."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def image_of(conn: AsyncConnection, chat_id: int):
    """Chat ``chat_id``'s image: a row (image_sha256, image_size), both None without one."""
    c = tables.chats
    return (await conn.execute(select(c.c.image_sha256, c.c.image_size).where(c.c.id == chat_id))).one()


async def set_image(conn: AsyncConnection, chat_id: int, sha256: str | None, size: int | None) -> None:
    """Make the blob ``sha256`` of ``size`` bytes (None: none) chat ``chat_id``'s image."""
    c = tables.chats
    await conn.execute(update(c).values(image_sha256=sha256, image_size=size).where(c.c.id == chat_id))
