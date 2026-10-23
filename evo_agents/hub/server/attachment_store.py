"""The rows of the files people attach to messages (``evo_agents.hub.attachments``): recording an upload, taking the
uploads a message names, tying them to the message, and listing the files of messages.

``take`` checks, with each row locked, that every id a message names is an upload of its sender, for that very run
or chat, that no message holds yet; ``hold_in_inbox`` and ``hold_in_chat`` then set the message that holds them, in
the transaction that writes the message. ``link_inbox`` ties files to a run_inbox row: the owner's own files, and
those of a chat message that a chat run takes in its inbox (``chat_runs._to_inbox``).
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.runs.models.attachment_parts import Attachment


def columns() -> tuple:
    a = tables.attachments
    return (a.c.id, a.c.name, a.c.media_type, a.c.size, a.c.sha256)


def view(row) -> Attachment:
    return Attachment(id=row.id, name=row.name, media_type=row.media_type, size=row.size, sha256=row.sha256)


async def record(conn: AsyncConnection, user_id: int, found: dict, *, run_id=None, chat_id=None) -> Attachment:
    """The row of an upload of ``user_id`` for run ``run_id`` or chat ``chat_id``; ``found`` holds its name,
    media_type, size and sha256."""
    a = tables.attachments
    added = insert(a).values(uploaded_by=user_id, run_id=run_id, chat_id=chat_id, **found).returning(*columns())
    return view((await conn.execute(added)).one())


def _refusal(attachment_id: int, row, user_id: int, run_id, chat_id) -> str | None:
    if row is None or row.uploaded_by != user_id:
        return f"attachment {attachment_id} is not an upload of yours: upload the file first (POST .../attachments)"
    if (row.run_id, row.chat_id) != (run_id, chat_id):
        where = f"run {row.run_id}" if row.run_id is not None else f"chat {row.chat_id}"
        return f"attachment {attachment_id} was uploaded for {where}: upload the file again for this message"
    if row.inbox_id is not None or row.chat_message_id is not None:
        return f"attachment {attachment_id} was sent already, with another message: upload the file again"
    return None


async def take(conn: AsyncConnection, ids: list[int], user_id: int, *, run_id=None, chat_id=None) -> list[Attachment]:
    """The uploads ``ids`` names, in its order, their rows locked, when each is an upload of ``user_id`` for run
    ``run_id`` or chat ``chat_id`` that no message holds yet; 422 naming the first that is not."""
    if not ids:
        return []
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "a message names each attachment once")
    a = tables.attachments
    query = select(*columns(), a.c.uploaded_by, a.c.run_id, a.c.chat_id, a.c.inbox_id, a.c.chat_message_id)
    rows = {row.id: row for row in await conn.execute(query.where(a.c.id.in_(ids)).order_by(a.c.id).with_for_update())}
    for attachment_id in ids:
        why = _refusal(attachment_id, rows.get(attachment_id), user_id, run_id, chat_id)
        if why is not None:
            raise HTTPException(422, why)
    return [view(rows[attachment_id]) for attachment_id in ids]


async def link_inbox(conn: AsyncConnection, inbox_id: int, files: list[Attachment]) -> None:
    """Tie ``files`` to run_inbox row ``inbox_id``, so the worker's inbox hands them out with its message."""
    if files:
        rows = [{"inbox_id": inbox_id, "attachment_id": f.id} for f in files]
        await conn.execute(insert(tables.run_inbox_attachments), rows)


async def hold_in_inbox(conn: AsyncConnection, inbox_id: int, files: list[Attachment]) -> None:
    """Set run_inbox row ``inbox_id`` as the message that holds ``files`` (``take`` checked them), and tie them to
    it."""
    if files:
        a = tables.attachments
        await conn.execute(update(a).values(inbox_id=inbox_id).where(a.c.id.in_([f.id for f in files])))
        await link_inbox(conn, inbox_id, files)


async def hold_in_chat(conn: AsyncConnection, message_id: int, files: list[Attachment]) -> None:
    """Set chat message ``message_id`` as the message that holds ``files`` (``take`` checked them)."""
    if files:
        a = tables.attachments
        await conn.execute(update(a).values(chat_message_id=message_id).where(a.c.id.in_([f.id for f in files])))


async def of_messages(conn: AsyncConnection, message_ids) -> dict[int, list[Attachment]]:
    """The files of each chat message of ``message_ids`` that carries any, in the order they were uploaded."""
    ids = sorted(set(message_ids))
    if not ids:
        return {}
    a = tables.attachments
    query = select(a.c.chat_message_id, *columns()).where(a.c.chat_message_id.in_(ids)).order_by(a.c.id)
    found: dict[int, list[Attachment]] = {}
    for row in await conn.execute(query):
        found.setdefault(row.chat_message_id, []).append(view(row))
    return found


async def of_inbox(conn: AsyncConnection, inbox_ids) -> dict[int, list[Attachment]]:
    """The files tied to each run_inbox row of ``inbox_ids`` that has any, in the order they were uploaded."""
    ids = sorted(set(inbox_ids))
    if not ids:
        return {}
    a, k = tables.attachments, tables.run_inbox_attachments
    query = select(k.c.inbox_id, *columns()).join_from(k, a, a.c.id == k.c.attachment_id)
    found: dict[int, list[Attachment]] = {}
    for row in await conn.execute(query.where(k.c.inbox_id.in_(ids)).order_by(a.c.id)):
        found.setdefault(row.inbox_id, []).append(view(row))
    return found
