"""Receiving an upload and taking it back: the hub streams the request's body into a temporary file while it counts
and hashes it (413 past MAX_FILE_BYTES), puts it in the blob store and records it for its run or chat, the check of
who may upload run before the bytes are read and again in the transaction that records them; the uploader takes back
an upload no message holds (404 for anyone else's, 409 for one sent)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
from pathlib import Path as FilePath

from fastapi import HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import attachments, blob_gc, outputs, runs
from evo_agents.hub.blobs import BlobStoreUnavailable
from evo_agents.hub.output_prune import delete_objects
from evo_agents.hub.server.attachments import repository
from evo_agents.hub.server.base.errors import Conflict, NotFound, Unprocessable
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.chats.service.access import chat_access
from evo_agents.hub.server.runs.models.attachment_parts import Attachment
from evo_agents.hub.server.runs.service.controls import owned_run
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)

UNAVAILABLE = "the blob store did not answer; nothing was kept, upload the file again shortly"


def no_file(attachment_id: int) -> NotFound:
    return NotFound(f"no attachment {attachment_id} here that you may read")


# Uploading


def _media_type(request: Request, name: str) -> str:
    """The media type of the upload: its Content-Type without parameters, unless it is missing, generic or not one
    type/subtype, when the name's extension says it."""
    declared = request.headers.get("content-type", "").split(";", 1)[0]
    try:
        found = outputs.check_media_type(declared)
    except ValueError:
        return outputs.guess_media_type(name)
    return outputs.guess_media_type(name) if found == "application/octet-stream" else found


async def _receive(request: Request, path: FilePath) -> tuple[str, int]:
    """Stream the request's body into ``path``, hashing it: (sha256, size); 413 past MAX_FILE_BYTES."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > attachments.MAX_FILE_BYTES:
        raise HTTPException(413, attachments.too_large(int(declared)))
    digest, size = hashlib.sha256(), 0
    with path.open("wb") as handle:
        async for chunk in request.stream():
            size += len(chunk)
            if size > attachments.MAX_FILE_BYTES:
                raise HTTPException(413, attachments.too_large(size))
            digest.update(chunk)
            handle.write(chunk)
    return digest.hexdigest(), size


def _checked_name(name: str) -> str:
    try:
        return attachments.check_name(name)
    except ValueError as error:
        raise Unprocessable(str(error)) from None


async def _upload(request: Request, user: Principal, name: str, check, **target) -> Attachment:
    """Receive the file, put it in the blob store and record it for ``target`` (run_id or chat_id), ``check`` run
    before the bytes are read and again in the transaction that records them."""
    name = _checked_name(name)
    store = blob_store(request)
    engine = request.app.state.engine
    async with UnitOfWork(engine) as conn:
        await check(conn)
    with tempfile.TemporaryDirectory() as directory:
        path = FilePath(directory) / "upload"
        sha256, size = await _receive(request, path)
        found = {"name": name, "media_type": _media_type(request, name), "size": size, "sha256": sha256}
        try:
            await asyncio.to_thread(store.put_file, sha256, path)
            async with UnitOfWork(engine) as conn:
                await check(conn)
                await blob_gc.lock_shared(conn)
                if await blob_gc.revive(conn, [sha256]):  # its object may be gone: put it back before the row commits
                    await asyncio.to_thread(store.put_file, sha256, path, replace=True)
                kept = await repository.record(conn, user.user_id, found, **target)
        except BlobStoreUnavailable:
            raise HTTPException(503, UNAVAILABLE) from None
    log.info("attachment uploaded", extra={"attachment_id": kept.id, "size": size, "login": user.login, **target})
    return kept


async def _messageable(conn: AsyncConnection, user: Principal, project: str, run_id: int) -> None:
    """The run's owner, while a message may go to the run (``run_events.send_message``); 403, 404 or 409 otherwise."""
    _, row = await owned_run(conn, user, project, run_id, "attach a file to a message to")
    resumes = row.kind in ("author", "chat") and row.state == "parked"
    if row.state not in runs.MESSAGE_STATES and not resumes:
        raise Conflict(f"run {run_id} is {row.state}: it takes no message, so no file for one")


async def to_run(request: Request, user: Principal, project: str, run_id: int, name: str) -> Attachment:
    """Upload a file for a message to the agent of run ``run_id``: its owner, while the run takes messages."""

    async def check(conn: AsyncConnection) -> None:
        await _messageable(conn, user, project, run_id)

    return await _upload(request, user, name, check, run_id=run_id)


async def to_chat(request: Request, user: Principal, chat_id: int, name: str) -> Attachment:
    """Upload a file for a message of chat ``chat_id``: a member of it."""

    async def check(conn: AsyncConnection) -> None:
        await chat_access(conn, user, chat_id)

    return await _upload(request, user, name, check, chat_id=chat_id)


# Taking an upload back


async def _take_back(request: Request, user: Principal, attachment_id: int, target) -> Response:
    """Delete the caller's upload ``attachment_id`` for ``target`` when no message holds it; 404 for anyone else's,
    409 for one sent."""
    engine = request.app.state.engine
    async with UnitOfWork(engine) as conn:
        await blob_gc.lock_exclusive(conn)
        row = await repository.own_upload(conn, attachment_id, user.user_id, target)
        if row is None:
            raise no_file(attachment_id)
        if row.chat_message_id is not None or row.inbox_id is not None:
            raise Conflict(f"attachment {attachment_id} was sent with a message: it stays with it")
        await repository.drop_upload(conn, attachment_id)
    await delete_objects(engine, request.app.state.blobs)
    return Response(status_code=204)


async def take_back_from_run(request: Request, user: Principal, project: str, run_id: int, attachment_id: int):
    """Take back an upload of the caller's for run ``run_id`` that no message holds: the run's owner."""
    async with UnitOfWork(request.app.state.engine) as conn:
        await owned_run(conn, user, project, run_id, "take back a file for")
    return await _take_back(request, user, attachment_id, repository.of_run(run_id))


async def take_back_from_chat(request: Request, user: Principal, chat_id: int, attachment_id: int) -> Response:
    """Take back an upload of the caller's for chat ``chat_id`` that no message holds."""
    return await _take_back(request, user, attachment_id, repository.of_chat(chat_id))
