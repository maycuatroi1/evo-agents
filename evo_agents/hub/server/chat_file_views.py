"""How the files people attach to chat messages read (``chat_files``, ``evo_agents.hub.server.chat_files``): the
``ChatFile`` a message carries, with the chat's URL of its bytes, which its members who read the message fetch."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import outputs, tables
from evo_agents.hub.db import one_of


class ChatFile(BaseModel):
    id: int
    name: str = Field(description="the file's name, as its uploader's browser named it")
    media_type: str = Field(description="type/subtype, as the uploader's browser named it")
    viewer: Literal[outputs.VIEWERS] = Field(description="what the web opens it with; none offers a download only")
    size: int = Field(description="bytes")
    sha256: str
    uploaded_by: str | None = Field(description="the login of the member who uploaded it; null once they are gone")
    created_at: datetime
    url: str = Field(description="where this hub answers its bytes to the chat's members; ?download=true to save it")


def file_url(chat_id: int, file_id: int) -> str:
    return f"/v1/chats/{chat_id}/files/{file_id}/content"


def file_select():
    f, u = tables.chat_files, tables.users
    return select(
        f.c.id,
        f.c.chat_id,
        f.c.message_id,
        f.c.user_id,
        f.c.name,
        f.c.media_type,
        f.c.size,
        f.c.sha256,
        f.c.created_at,
        u.c.login.label("uploaded_by"),
    ).select_from(f.outerjoin(u, u.c.id == f.c.user_id))


def file_view(row) -> ChatFile:
    return ChatFile(
        id=row.id,
        name=row.name,
        media_type=row.media_type,
        viewer=outputs.viewer(row.media_type),
        size=row.size,
        sha256=row.sha256,
        uploaded_by=row.uploaded_by,
        created_at=row.created_at,
        url=file_url(row.chat_id, row.id),
    )


async def by_message(conn: AsyncConnection, message_ids: list[int]) -> dict[int, list[ChatFile]]:
    """The files each of ``message_ids`` carries, in the order they were uploaded."""
    if not message_ids:
        return {}
    f = tables.chat_files
    query = file_select().where(one_of(f.c.message_id, message_ids)).order_by(f.c.id)
    found: dict[int, list[ChatFile]] = defaultdict(list)
    for row in await conn.execute(query):
        found[row.message_id].append(file_view(row))
    return found
