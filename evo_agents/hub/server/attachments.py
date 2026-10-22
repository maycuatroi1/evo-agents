"""The routes of the files people attach to messages (``evo_agents.hub.attachments``; docs/hub.md, section
"Attachments"):

- POST /v1/projects/{p}/runs/{id}/attachments?name=, the run's owner, while the run takes messages (queued, held by
  its worker, or a parked author or chat run, which a message resumes);
- POST /v1/chats/{id}/attachments?name=, a member of the chat;

  the request's body is the file's bytes, its Content-Type the file's media type (guessed from the name when it is
  missing, application/octet-stream or not one type/subtype). The hub streams the bytes into a temporary file while it
  counts and hashes them, refuses a file over MAX_FILE_BYTES with 413 (by Content-Length when it says so, else as the
  bytes pass the limit), puts them in the blob store, and answers 201 with the upload: its id goes in the message's
  ``attachment_ids``.
- DELETE /v1/projects/{p}/runs/{id}/attachments/{aid} and DELETE /v1/chats/{id}/attachments/{aid}: the uploader takes
  back an upload no message holds; 204.
- GET /v1/projects/{p}/runs/{id}/attachments/{aid}/content, to the run's readers (``views.readable_run``, by sink),
  for a file of a message to the run (or an upload of the caller's own not sent yet);
- GET /v1/chats/{id}/attachments/{aid}/content, to a member who clears the label of the message that carries it (or
  the uploader, for an upload not sent yet);
- GET /v1/worker/runs/{id}/attachments/{aid}/content, to the worker that claimed the run: a file of a message the run's
  inbox took, or of a chat message the run's agent may read (``chat_read``).

The bytes are served as an output's are (``output_content.serve``): nosniff, a safe Content-Disposition, one Range,
and a sandbox Content-Security-Policy for HTML and SVG. Anyone else gets the 404 of a file that does not exist.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
from pathlib import Path as FilePath
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import attachments, blob_gc, outputs, runs, tables
from evo_agents.hub.attachment_prune import drop
from evo_agents.hub.blobs import BlobStoreUnavailable
from evo_agents.hub.output_prune import delete_objects
from evo_agents.hub.server import attachment_store
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.chat_access import chat_access, label_reader
from evo_agents.hub.server.chat_members import ChatId
from evo_agents.hub.server.chat_read import readable, run_readers
from evo_agents.hub.server.chat_views import message_select
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.output_content import CONTENT, Download, IfNoneMatch, RangeHeader, Sink, serve
from evo_agents.hub.server.run_events import claimed_run
from evo_agents.hub.server.runs.models import MAX_ID, RunId
from evo_agents.hub.server.runs.models.attachment_parts import Attachment
from evo_agents.hub.server.runs.service.controls import owned_run
from evo_agents.hub.server.runs.service.views import readable_run
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

AttachmentId = Annotated[int, Path(ge=1, le=MAX_ID)]
FileName = Annotated[str, Query(min_length=1, max_length=attachments.MAX_NAME_CHARS, description="the file's name")]
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 413, 422, 503)}
DROP_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409)}
BODY = {
    "requestBody": {
        "required": True,
        "description": f"the file's bytes, at most {attachments.MAX_FILE_BYTES}; Content-Type is its media type",
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
    }
}
UNAVAILABLE = "the blob store did not answer; nothing was kept, upload the file again shortly"

router = APIRouter(prefix="/v1/projects", tags=["runs"], responses={401: {"model": ErrorBody}})
chat_router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


def _no_file(attachment_id: int) -> HTTPException:
    return HTTPException(404, f"no attachment {attachment_id} here that you may read")


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
        raise HTTPException(422, str(error)) from None


async def _upload(request: Request, user: Principal, name: str, check, **target) -> Attachment:
    """Receive the file, put it in the blob store and record it for ``target`` (run_id or chat_id), ``check`` run
    before the bytes are read and again in the transaction that records them."""
    name = _checked_name(name)
    store = blob_store(request)
    engine = request.app.state.engine
    async with engine.begin() as conn:
        await check(conn)
    with tempfile.TemporaryDirectory() as directory:
        path = FilePath(directory) / "upload"
        sha256, size = await _receive(request, path)
        found = {"name": name, "media_type": _media_type(request, name), "size": size, "sha256": sha256}
        try:
            await asyncio.to_thread(store.put_file, sha256, path)
            async with engine.begin() as conn:
                await check(conn)
                await blob_gc.lock_shared(conn)
                if await blob_gc.revive(conn, [sha256]):  # its object may be gone: put it back before the row commits
                    await asyncio.to_thread(store.put_file, sha256, path, replace=True)
                kept = await attachment_store.record(conn, user.user_id, found, **target)
        except BlobStoreUnavailable:
            raise HTTPException(503, UNAVAILABLE) from None
    log.info("attachment uploaded", extra={"attachment_id": kept.id, "size": size, "login": user.login, **target})
    return kept


async def _messageable(conn: AsyncConnection, user: Principal, project: str, run_id: int) -> None:
    """The run's owner, while a message may go to the run (``run_events.send_message``); 403, 404 or 409 otherwise."""
    _, row = await owned_run(conn, user, project, run_id, "attach a file to a message to")
    resumes = row.kind in ("author", "chat") and row.state == "parked"
    if row.state not in runs.MESSAGE_STATES and not resumes:
        raise HTTPException(409, f"run {run_id} is {row.state}: it takes no message, so no file for one")


@router.post(
    "/{project}/runs/{run_id}/attachments",
    status_code=201,
    response_model=Attachment,
    responses=REFUSALS,
    openapi_extra=BODY,
)
async def upload_to_run(
    request: Request, project: ProjectName, run_id: RunId, user: CurrentUser, name: FileName
) -> Attachment:
    """Upload a file for a message to the run's agent: the run's owner, while the run takes messages."""

    async def check(conn: AsyncConnection) -> None:
        await _messageable(conn, user, project, run_id)

    return await _upload(request, user, name, check, run_id=run_id)


@chat_router.post(
    "/{chat_id}/attachments", status_code=201, response_model=Attachment, responses=REFUSALS, openapi_extra=BODY
)
async def upload_to_chat(request: Request, chat_id: ChatId, user: CurrentUser, name: FileName) -> Attachment:
    """Upload a file for a message of the chat: a member of it."""

    async def check(conn: AsyncConnection) -> None:
        await chat_access(conn, user, chat_id)

    return await _upload(request, user, name, check, chat_id=chat_id)


# Taking an upload back


async def _take_back(request: Request, user: Principal, attachment_id: int, target) -> Response:
    """Delete the caller's upload ``attachment_id`` for ``target`` when no message holds it; 404 for anyone else's,
    409 for one sent."""
    a = tables.attachments
    engine = request.app.state.engine
    async with engine.begin() as conn:
        await blob_gc.lock_exclusive(conn)
        query = select(a.c.chat_message_id, a.c.inbox_id).where(
            a.c.id == attachment_id, a.c.uploaded_by == user.user_id, target
        )
        row = (await conn.execute(query.with_for_update())).one_or_none()
        if row is None:
            raise _no_file(attachment_id)
        if row.chat_message_id is not None or row.inbox_id is not None:
            raise HTTPException(409, f"attachment {attachment_id} was sent with a message: it stays with it")
        await drop(conn, a.c.id == attachment_id)
    await delete_objects(engine, request.app.state.blobs)
    return Response(status_code=204)


@router.delete("/{project}/runs/{run_id}/attachments/{attachment_id}", status_code=204, responses=DROP_REFUSALS)
async def delete_run_upload(
    request: Request, project: ProjectName, run_id: RunId, attachment_id: AttachmentId, user: CurrentUser
) -> Response:
    """Take back an upload of yours for the run that no message holds."""
    async with request.app.state.engine.begin() as conn:
        await owned_run(conn, user, project, run_id, "take back a file for")
    return await _take_back(request, user, attachment_id, tables.attachments.c.run_id == run_id)


@chat_router.delete("/{chat_id}/attachments/{attachment_id}", status_code=204, responses=DROP_REFUSALS)
async def delete_chat_upload(
    request: Request, chat_id: ChatId, attachment_id: AttachmentId, user: CurrentUser
) -> Response:
    """Take back an upload of yours for the chat that no message holds."""
    return await _take_back(request, user, attachment_id, tables.attachments.c.chat_id == chat_id)


# Reading the bytes


async def _file(conn: AsyncConnection, attachment_id: int, *where):
    a = tables.attachments
    row = (await conn.execute(select(*attachment_store.columns()).where(a.c.id == attachment_id, *where))).one_or_none()
    if row is None:
        raise _no_file(attachment_id)
    return row


def _in_inbox_of(run_id: int):
    """Whether the attachment is tied to a message of the inbox of run ``run_id``."""
    a, k, i = tables.attachments, tables.run_inbox_attachments, tables.run_inbox
    return exists().where(k.c.attachment_id == a.c.id, k.c.inbox_id == i.c.id, i.c.run_id == run_id)


@router.get("/{project}/runs/{run_id}/attachments/{attachment_id}/content", response_class=Response, responses=CONTENT)
async def run_content(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    attachment_id: AttachmentId,
    user: CurrentUser,
    download: Download = False,
    range_: RangeHeader = None,
    if_none_match: IfNoneMatch = None,
    sink: Sink = None,
) -> Response:
    """The bytes of a file of a message to the run, for its readers."""
    a = tables.attachments
    own_upload = and_(a.c.run_id == run_id, a.c.uploaded_by == user.user_id)
    async with request.app.state.engine.begin() as conn:
        await readable_run(conn, user, project, run_id, sink)
        row = await _file(conn, attachment_id, or_(_in_inbox_of(run_id), own_upload))
    return await serve(request, row, download, range_, if_none_match)


@chat_router.get("/{chat_id}/attachments/{attachment_id}/content", response_class=Response, responses=CONTENT)
async def chat_content(
    request: Request,
    chat_id: ChatId,
    attachment_id: AttachmentId,
    user: CurrentUser,
    download: Download = False,
    range_: RangeHeader = None,
    if_none_match: IfNoneMatch = None,
) -> Response:
    """The bytes of a file a message of the chat carries, for a member who reads that message."""
    a, cm = tables.attachments, tables.chat_messages
    held = select(cm.c.label).join_from(a, cm, cm.c.id == a.c.chat_message_id)
    held = held.where(a.c.id == attachment_id, a.c.chat_id == chat_id)
    async with request.app.state.engine.begin() as conn:
        await chat_access(conn, user, chat_id)
        labels = list((await conn.execute(held)).scalars())
        if labels:
            reader = await label_reader(conn, user.user_id, labels)
            if not reader.sees(labels[0]):
                raise _no_file(attachment_id)
            row = await _file(conn, attachment_id)
        else:  # an upload not sent yet: its uploader's alone
            row = await _file(conn, attachment_id, a.c.chat_id == chat_id, a.c.uploaded_by == user.user_id)
    return await serve(request, row, download, range_, if_none_match)


async def _readable_by_run(conn: AsyncConnection, user: Principal, run_id: int, attachment_id: int) -> bool:
    """Whether the agent of run ``run_id`` may read the chat message that holds attachment ``attachment_id``."""
    a = tables.attachments
    found = select(a.c.chat_message_id).where(a.c.id == attachment_id, a.c.chat_message_id.is_not(None))
    message_id = (await conn.execute(found)).scalar_one_or_none()
    if message_id is None:
        return False
    rows = (await conn.execute(message_select().where(tables.chat_messages.c.id == message_id))).all()
    try:
        readers = await run_readers(conn, user, run_id)
    except HTTPException:
        return False
    return bool(await readable(conn, readers, rows, None))


@worker_router.get("/runs/{run_id}/attachments/{attachment_id}/content", response_class=Response, responses=CONTENT)
async def worker_content(
    request: Request,
    run_id: RunId,
    attachment_id: AttachmentId,
    user: CurrentUser,
    range_: RangeHeader = None,
    if_none_match: IfNoneMatch = None,
) -> Response:
    """The bytes of a file for the agent of a run the worker claimed: a file of a message its inbox took, or of a chat
    message the agent may read."""
    async with request.app.state.engine.begin() as conn:
        await claimed_run(conn, user, run_id)
        a = tables.attachments
        rows = (await conn.execute(select(a.c.id).where(a.c.id == attachment_id, _in_inbox_of(run_id)))).all()
        if not rows and not await _readable_by_run(conn, user, run_id, attachment_id):
            raise _no_file(attachment_id)
        row = await _file(conn, attachment_id)
    return await serve(request, row, True, range_, if_none_match)
