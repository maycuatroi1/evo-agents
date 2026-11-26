"""The files of messages, as the run's messages (``run_events``) and the chat's (``chats``) take them: ``take`` checks,
with each row locked, that every id a message names is an upload of its sender, for that very run or chat, that no
message holds yet; the message then holds them (``hold_in_inbox``, ``hold_in_chat``) in the transaction that writes it,
and ``of_inbox`` lists the files of a run's messages."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server.attachments import repository
from evo_agents.hub.server.base.errors import Unprocessable
from evo_agents.hub.server.runs.models.attachment_parts import Attachment


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
        raise Unprocessable("a message names each attachment once")
    rows = await repository.locked(conn, ids)
    for attachment_id in ids:
        why = _refusal(attachment_id, rows.get(attachment_id), user_id, run_id, chat_id)
        if why is not None:
            raise Unprocessable(why)
    return [repository.view(rows[attachment_id]) for attachment_id in ids]


async def hold_in_inbox(conn: AsyncConnection, inbox_id: int, files: list[Attachment]) -> None:
    """Have run_inbox row ``inbox_id`` hold ``files``, which ``take`` checked, and hand them out with its message."""
    await repository.hold_in_inbox(conn, inbox_id, files)


async def hold_in_chat(conn: AsyncConnection, message_id: int, files: list[Attachment]) -> None:
    """Have chat message ``message_id`` hold ``files``, which ``take`` checked."""
    await repository.hold_in_chat(conn, message_id, files)


async def of_inbox(conn: AsyncConnection, inbox_ids) -> dict[int, list[Attachment]]:
    """The files of each run_inbox row of ``inbox_ids`` that has any, in the order they were uploaded."""
    return await repository.of_inbox(conn, inbox_ids)
