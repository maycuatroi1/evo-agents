"""The rows of the files people attach to messages (``evo_agents.hub.attachments``): recording an upload, locking the
uploads a message names, tying them to the message, listing the files of messages, and finding the file a reader asks
for. Every query of the package on attachments and run_inbox_attachments is here.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import Select, and_, exists, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.attachment_prune import drop
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


async def locked(conn: AsyncConnection, ids: list[int]) -> dict:
    """The rows of uploads ``ids``, by id, locked: who uploaded each, for which run or chat, and the message that
    holds it."""
    a = tables.attachments
    query = select(*columns(), a.c.uploaded_by, a.c.run_id, a.c.chat_id, a.c.inbox_id, a.c.chat_message_id)
    return {row.id: row for row in await conn.execute(query.where(a.c.id.in_(ids)).order_by(a.c.id).with_for_update())}


async def link_inbox(conn: AsyncConnection, inbox_id: int, files: list[Attachment]) -> None:
    """Tie ``files`` to run_inbox row ``inbox_id``, so the worker's inbox hands them out with its message."""
    if files:
        rows = [{"inbox_id": inbox_id, "attachment_id": f.id} for f in files]
        await conn.execute(insert(tables.run_inbox_attachments), rows)


async def hold_in_inbox(conn: AsyncConnection, inbox_id: int, files: list[Attachment]) -> None:
    """Set run_inbox row ``inbox_id`` as the message that holds ``files`` (``service.messages.take`` checked them), and
    tie them to it."""
    if files:
        a = tables.attachments
        await conn.execute(update(a).values(inbox_id=inbox_id).where(a.c.id.in_([f.id for f in files])))
        await link_inbox(conn, inbox_id, files)


async def hold_in_chat(conn: AsyncConnection, message_id: int, files: list[Attachment]) -> None:
    """Set chat message ``message_id`` as the message that holds ``files`` (``service.messages.take`` checked them)."""
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


# Taking an upload back


def of_run(run_id: int):
    """Whether the attachment is an upload for run ``run_id``."""
    return tables.attachments.c.run_id == run_id


def of_chat(chat_id: int):
    """Whether the attachment is an upload for chat ``chat_id``."""
    return tables.attachments.c.chat_id == chat_id


async def own_upload(conn: AsyncConnection, attachment_id: int, user_id: int, target):
    """The row of upload ``attachment_id`` of ``user_id`` for ``target`` (``of_run``, ``of_chat``), locked: the
    message that holds it; None for anyone else's."""
    a = tables.attachments
    query = select(a.c.chat_message_id, a.c.inbox_id).where(a.c.id == attachment_id, a.c.uploaded_by == user_id, target)
    return (await conn.execute(query.with_for_update())).one_or_none()


async def drop_upload(conn: AsyncConnection, attachment_id: int) -> None:
    """Delete upload ``attachment_id``; its bytes go once nothing refers to them (``attachment_prune.drop``)."""
    await drop(conn, tables.attachments.c.id == attachment_id)


# Reading the bytes


async def file(conn: AsyncConnection, attachment_id: int, *where):
    """The row of attachment ``attachment_id`` that ``where`` keeps, as ``serve`` reads it; None when there is none."""
    a = tables.attachments
    return (await conn.execute(select(*columns()).where(a.c.id == attachment_id, *where))).one_or_none()


def in_inbox_of(run_id: int):
    """Whether the attachment is tied to a message of the inbox of run ``run_id``."""
    a, k, i = tables.attachments, tables.run_inbox_attachments, tables.run_inbox
    return exists().where(k.c.attachment_id == a.c.id, k.c.inbox_id == i.c.id, i.c.run_id == run_id)


async def run_file(conn: AsyncConnection, attachment_id: int, run_id: int, user_id: int):
    """Attachment ``attachment_id`` when a message to run ``run_id`` carries it, or it is an upload of ``user_id``
    for the run; None otherwise."""
    a = tables.attachments
    own = and_(a.c.run_id == run_id, a.c.uploaded_by == user_id)
    return await file(conn, attachment_id, or_(in_inbox_of(run_id), own))


async def unsent_file(conn: AsyncConnection, attachment_id: int, chat_id: int, user_id: int):
    """Attachment ``attachment_id`` when it is an upload of ``user_id`` for chat ``chat_id``; None otherwise."""
    a = tables.attachments
    return await file(conn, attachment_id, a.c.chat_id == chat_id, a.c.uploaded_by == user_id)


async def held_labels(conn: AsyncConnection, chat_id: int, attachment_id: int) -> list:
    """The label of the message of chat ``chat_id`` that holds attachment ``attachment_id``; none for one not sent."""
    a, cm = tables.attachments, tables.chat_messages
    held = select(cm.c.label).join_from(a, cm, cm.c.id == a.c.chat_message_id)
    held = held.where(a.c.id == attachment_id, a.c.chat_id == chat_id)
    return list((await conn.execute(held)).scalars())


async def in_inbox(conn: AsyncConnection, run_id: int, attachment_id: int) -> bool:
    """Whether attachment ``attachment_id`` is tied to a message of the inbox of run ``run_id``."""
    a = tables.attachments
    rows = (await conn.execute(select(a.c.id).where(a.c.id == attachment_id, in_inbox_of(run_id)))).all()
    return bool(rows)


async def holding_message(conn: AsyncConnection, attachment_id: int, message_select: Callable[[], Select]) -> list:
    """The rows (``message_select``, chat_views' query of messages) of the chat message that holds attachment
    ``attachment_id``; none for a file no chat message holds."""
    a = tables.attachments
    found = select(a.c.chat_message_id).where(a.c.id == attachment_id, a.c.chat_message_id.is_not(None))
    message_id = (await conn.execute(found)).scalar_one_or_none()
    if message_id is None:
        return []
    return list((await conn.execute(message_select().where(tables.chat_messages.c.id == message_id))).all())
