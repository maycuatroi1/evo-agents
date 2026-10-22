"""The files people attach to chat messages (``chat_files``): POST /v1/chats/{id}/files and GET
/v1/chats/{id}/files/{fid}/content.

A member uploads a file before sending the message that carries it: POST .../files?name=NAME with the file's bytes as
the request's body and its media type as Content-Type (``application/octet-stream`` when none is given), at most
``chats.MAX_FILE_BYTES`` (413 over it, read from Content-Length before the body when it is given). The hub stores the
bytes in the blob store as a blob of its own hashing and answers the ``ChatFile``, which no message carries yet. The
member then names it in ``file_ids`` of POST .../messages (``attach``): a message carries at most
``chats.MAX_MESSAGE_FILES`` files, each uploaded to that chat by its sender and carried by no message yet (422
otherwise). A file no message carried a day after it was uploaded (UNSENT_AFTER) is deleted the next time its uploader
uploads one, and its blob recorded for deletion when nothing else refers to it (``blob_gc.forget``).

GET .../content answers the bytes to a member of the chat who reads the message carrying the file (its label cleared),
or to its uploader before it is sent, under the headers of a run's outputs (``output_content.serve``: the
Content-Security-Policy of ``outputs.content_policy``, nosniff, a range, an attachment with ``download=true``). Anyone
else gets the 404 of a file that does not exist. The agent of a chat run reads the files of the messages of its chat
through the worker's route (``chat_agents``, ``evo-agents worker chat file``).

An upload is audited as chat.file ("chat:<id> file:<id> sha256:<the first 16 of its SHA-256>"), never with its name.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request, Response
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, chats, outputs, tables
from evo_agents.hub import chat_runs as rules
from evo_agents.hub.blobs import BlobStoreUnavailable
from evo_agents.hub.db import one_of
from evo_agents.hub.server import audit
from evo_agents.hub.server.blobs import blob_store, put_bytes
from evo_agents.hub.server.chat_access import ChatAccess, chat_access, label_reader
from evo_agents.hub.server.chat_file_views import ChatFile, file_select, file_view
from evo_agents.hub.server.chat_members import ChatId
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.output_content import CONTENT, Download, IfNoneMatch, RangeHeader, serve
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

FileId = Annotated[int, Path(ge=1, le=MAX_ID)]
UNSENT_AFTER = timedelta(days=1)  # a file no message carries is deleted after this
DEFAULT_TYPE = "application/octet-stream"
UNAVAILABLE = "the blob store did not answer; the file was not uploaded, try again shortly"
UPLOAD_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 413, 422, 503)}

router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})


def _too_large(size: int) -> HTTPException:
    return HTTPException(413, f"the file is {size} bytes, over the {chats.MAX_FILE_BYTES} a chat file may have")


def _no_file(file_id: int) -> HTTPException:
    return HTTPException(404, f"no file {file_id} here that you may read")


def checked_name(name: str) -> str:
    """``name`` as a chat file keeps it: a file's name, its directories left out; 422 for none."""
    try:
        return outputs.check_name(name.replace("\\", "/").rsplit("/", 1)[-1])
    except ValueError as exc:
        raise HTTPException(422, str(exc).replace("an output", "a file")) from None


def checked_type(content_type: str | None) -> str:
    """The media type of Content-Type ``content_type`` without its parameters; DEFAULT_TYPE for none, 422 for one
    that is no ``type/subtype``."""
    found = (content_type or "").split(";", 1)[0].strip()
    if not found:
        return DEFAULT_TYPE
    try:
        return outputs.check_media_type(found)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


async def _body(request: Request, declared: str | None) -> bytes:
    if declared is not None and declared.isdigit() and int(declared) > chats.MAX_FILE_BYTES:
        raise _too_large(int(declared))
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > chats.MAX_FILE_BYTES:
            raise _too_large(len(data))
    if not data:
        raise HTTPException(422, "the file is empty: a chat file has some bytes")
    return bytes(data)


async def _drop_unsent(conn: AsyncConnection, user_id: int) -> int:
    """Delete the files of ``user_id`` no message carried within UNSENT_AFTER, recording their blobs for deletion when
    nothing else refers to them; how many. Holding the blob lock exclusively."""
    f = tables.chat_files
    stale = (
        delete(f)
        .where(f.c.user_id == user_id, f.c.message_id.is_(None), f.c.created_at < func.now() - UNSENT_AFTER)
        .returning(f.c.sha256, f.c.size)
    )
    gone = (await conn.execute(stale)).all()
    if gone:
        await blob_gc.forget(conn, {row.sha256: (row.size, chats.FILE_KIND) for row in gone})
    return len(gone)


async def _store(conn: AsyncConnection, store, chat_id: int, user: Principal, name: str, media_type: str, data: bytes):
    """The row (``file_select``) of the file just uploaded, its bytes put back when their deletion was pending, and the
    uploader's stale files dropped; holding the blob lock exclusively."""
    f = tables.chat_files
    sha256 = hashlib.sha256(data).hexdigest()
    if await blob_gc.revive(conn, [sha256]):
        await asyncio.to_thread(put_bytes, store, sha256, data, replace=True)
    values = {"chat_id": chat_id, "user_id": user.user_id, "name": name, "media_type": media_type}
    values |= {"size": len(data), "sha256": sha256}
    file_id = (await conn.execute(insert(f).values(values).returning(f.c.id))).scalar_one()
    await _drop_unsent(conn, user.user_id)
    target = f"chat:{chat_id} file:{file_id} sha256:{sha256[:16]}"
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.CHAT_FILE, target=target)
    return (await conn.execute(file_select().where(f.c.id == file_id))).one()


@router.post("/{chat_id}/files", status_code=201, response_model=ChatFile, responses=UPLOAD_REFUSALS)
async def upload_file(
    request: Request,
    chat_id: ChatId,
    user: CurrentUser,
    name: Annotated[str, Query(min_length=1, max_length=outputs.MAX_NAME_CHARS, description="the file's name")],
    content_type: Annotated[str | None, Header(description="the file's media type")] = None,
    content_length: Annotated[str | None, Header(description="the file's size in bytes")] = None,
) -> ChatFile:
    """Upload a file to attach to a message of the chat: the request's body is the file. The message that names it in
    ``file_ids`` carries it."""
    name, media_type = checked_name(name), checked_type(content_type)
    async with request.app.state.engine.begin() as conn:  # the refusals of the caller before the bytes are read
        await chat_access(conn, user, chat_id)
    data = await _body(request, content_length)
    store = blob_store(request)
    try:
        await asyncio.to_thread(put_bytes, store, hashlib.sha256(data).hexdigest(), data)
        async with request.app.state.engine.begin() as conn:
            await blob_gc.lock_exclusive(conn)  # before the chat's row, as a chat's image is set
            await chat_access(conn, user, chat_id)
            row = await _store(conn, store, chat_id, user, name, media_type, data)
    except BlobStoreUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None
    log.info("chat file uploaded", extra={"chat_id": chat_id, "file_id": row.id, "size": row.size})
    return file_view(row)


async def attach(
    conn: AsyncConnection, chat: ChatAccess, user: Principal, file_ids: list[int], message_id: int
) -> list[dict]:
    """Make message ``message_id`` of ``chat`` carry the files ``file_ids``, which ``user`` uploaded to that chat and no
    message carries yet, 422 for any other; the files as the prompt of a chat run names them (``chat_runs.file_fact``),
    in the order they were uploaded."""
    wanted = sorted(set(file_ids))
    if not wanted:
        return []
    f = tables.chat_files
    claimed = (
        update(f)
        .values(message_id=message_id)
        .where(
            one_of(f.c.id, wanted),
            f.c.chat_id == chat.chat_id,
            f.c.user_id == user.user_id,
            f.c.message_id.is_(None),
        )
        .returning(f.c.id, f.c.name, f.c.media_type, f.c.size)
    )
    taken = {row.id: row for row in await conn.execute(claimed)}
    missing = [str(file_id) for file_id in wanted if file_id not in taken]
    if missing:
        raise HTTPException(
            422,
            f"no file {', '.join(missing)} you uploaded to chat {chat.chat_id} waits for a message: upload it again",
        )
    return [rules.file_fact(row.id, row.name, row.media_type, row.size) for _, row in sorted(taken.items())]


async def readable_file(conn: AsyncConnection, chat_id: int, file_id: int, user_id: int, *, own_unsent: bool = True):
    """The row (``file_select``) of file ``file_id`` of chat ``chat_id`` when person ``user_id`` may read it: they clear
    the label of the message that carries it, or, with ``own_unsent``, uploaded it and no message carries it yet. 404
    otherwise."""
    f, cm = tables.chat_files, tables.chat_messages
    row = (await conn.execute(file_select().where(f.c.id == file_id, f.c.chat_id == chat_id))).one_or_none()
    if row is None:
        raise _no_file(file_id)
    if row.message_id is None:
        if not own_unsent or row.user_id != user_id:
            raise _no_file(file_id)
        return row
    label = (await conn.execute(select(cm.c.label).where(cm.c.id == row.message_id))).scalar_one()
    if not (await label_reader(conn, user_id, [label])).sees(label):
        raise _no_file(file_id)
    return row


@router.get("/{chat_id}/files/{file_id}/content", response_class=Response, responses=CONTENT)
async def file_content(
    request: Request,
    chat_id: ChatId,
    file_id: FileId,
    user: CurrentUser,
    download: Download = False,
    range_: RangeHeader = None,
    if_none_match: IfNoneMatch = None,
) -> Response:
    """The bytes of a file a message of the chat carries, for a member who reads that message."""
    async with request.app.state.engine.begin() as conn:
        await chat_access(conn, user, chat_id)
        row = await readable_file(conn, chat_id, file_id, user.user_id)
    return await serve(request, row, download, range_, if_none_match)
