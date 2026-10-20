"""The image of a group chat (``evo_agents.hub.chats``): PUT, DELETE and GET /v1/chats/{id}/image.

Any member of a group chat sets its image or takes it off: a PNG, JPEG or WebP image of at most
``agent_profiles.AVATAR_MAX_BYTES``, sent as base64 in JSON and checked by its first bytes, as an agent's avatar is
(413 over the size, 422 for anything else). Someone who is not a member gets the 404 of a chat that does not exist, as
for every route of a chat. A direct chat takes no image (409): its face is its agent's avatar.

The hub stores the image in the blob store, and the chat's row refers to it (``chats.image_sha256``), which
``blob_gc`` counts as a reference, so the collection of unreferenced blobs leaves it. Setting another image or taking
it off drops the old reference in a transaction holding the blob lock exclusively, and ``blob_gc.forget`` records the
old blob for deletion when nothing else refers to it, another chat with the same image included; an image set again
while its deletion is pending is put back first (``blob_gc.revive``). GET serves the image to the chat's members only,
with its SHA-256 as ETag (304 when it matches); its URL (``Chat.image_url``, ``ChatSummary.image_url``) carries a part
of it, so a browser keeps it until it changes.

Each change is audited as chat.image ("chat:<id> image:<the first 16 of its SHA-256>", or ``image:none``) and is an
image_changed event of the chat, so its other members' pages show it at once. Setting the image a chat has already
changes nothing.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, chats, tables
from evo_agents.hub.blobs import BlobStoreUnavailable
from evo_agents.hub.server import audit
from evo_agents.hub.server.agent_profiles import AVATAR_MAX_BYTES, AVATAR_TYPES, AvatarUpload, media_type
from evo_agents.hub.server.blobs import blob_store, fetch_bytes, put_bytes
from evo_agents.hub.server.chat_access import ChatAccess, chat_access
from evo_agents.hub.server.chat_members import ChatId
from evo_agents.hub.server.chat_store import write_event
from evo_agents.hub.server.chat_views import Chat, chat_view
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

IMAGE_KIND = "chat-image"  # the kind blob_deletions records an image no chat refers to any longer with
UNAVAILABLE = "the blob store did not answer; the chat's image was not changed, try again shortly"
SET_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 413, 422, 503)}
DROP_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409)}
READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 503)}

router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})


def _group(chat: ChatAccess) -> ChatAccess:
    if chat.kind == chats.DIRECT:
        raise HTTPException(
            409,
            f"chat {chat.chat_id} is a direct chat, whose face is its agent's avatar: it takes no image of its own",
        )
    return chat


def _image(body: AvatarUpload) -> tuple[bytes, str]:
    data = base64.b64decode(body.data, validate=True)
    if len(data) > AVATAR_MAX_BYTES:
        raise HTTPException(413, f"the image is {len(data)} bytes, over the {AVATAR_MAX_BYTES} a chat's image may have")
    if media_type(data) is None:
        raise HTTPException(
            422, f"a chat's image is a {', '.join(AVATAR_TYPES.values())} image, and this one is none; nothing changed"
        )
    return data, hashlib.sha256(data).hexdigest()


async def _change(conn: AsyncConnection, store, chat: ChatAccess, user: Principal, image, data: bytes) -> bool:
    """Make ``image`` ((SHA-256, size), or None) the chat's image, holding the blob lock exclusively: put its bytes
    back when their deletion was pending, record the old blob for deletion when nothing else refers to it, and write
    the event and the audit row. Whether it changed."""
    c = tables.chats
    old = (await conn.execute(select(c.c.image_sha256, c.c.image_size).where(c.c.id == chat.chat_id))).one()
    sha256, size = image if image is not None else (None, None)
    if old.image_sha256 == sha256:
        return False
    if sha256 is not None and await blob_gc.revive(conn, [sha256]):
        await asyncio.to_thread(put_bytes, store, sha256, data, replace=True)
    await conn.execute(update(c).values(image_sha256=sha256, image_size=size).where(c.c.id == chat.chat_id))
    if old.image_sha256 is not None:
        await blob_gc.forget(conn, {old.image_sha256: (old.image_size, IMAGE_KIND)})
    await write_event(conn, chat.chat_id, chats.IMAGE_CHANGED, {"sha256": sha256, "by": user.login})
    target = f"chat:{chat.chat_id} image:{sha256[:16] if sha256 else 'none'}"
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.CHAT_IMAGE, target=target)
    return True


async def _set(request: Request, chat_id: int, user: Principal, image, data: bytes = b"") -> Chat:
    store = blob_store(request) if image is not None else None
    try:
        async with request.app.state.engine.begin() as conn:
            await blob_gc.lock_exclusive(conn)  # before the chat's row, so its messages never wait for the lock
            chat = _group(await chat_access(conn, user, chat_id, lock=True))
            changed = await _change(conn, store, chat, user, image, data)
            view = await chat_view(conn, chat)
    except BlobStoreUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None
    log.info("chat image changed", extra={"chat_id": chat_id, "set": image is not None, "changed": changed})
    return view


@router.put("/{chat_id}/image", response_model=Chat, responses=SET_REFUSALS)
async def put_image(request: Request, chat_id: ChatId, body: AvatarUpload, user: CurrentUser) -> Chat:
    """Set the group chat's image, a PNG, JPEG or WebP image: its members see it in their list and its header."""
    async with request.app.state.engine.begin() as conn:  # the refusals of the caller before the bytes are stored
        _group(await chat_access(conn, user, chat_id))
    data, sha256 = _image(body)
    try:
        await asyncio.to_thread(put_bytes, blob_store(request), sha256, data)
    except BlobStoreUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None
    return await _set(request, chat_id, user, (sha256, len(data)), data)


@router.delete("/{chat_id}/image", response_model=Chat, responses=DROP_REFUSALS)
async def delete_image(request: Request, chat_id: ChatId, user: CurrentUser) -> Chat:
    """Take the group chat's image off: the web draws the faces of its members again."""
    return await _set(request, chat_id, user, None)


def _matches(if_none_match: str | None, etag: str) -> bool:
    if if_none_match is None:
        return False
    tags = [tag.strip().removeprefix("W/") for tag in if_none_match.split(",")]
    return "*" in tags or etag in tags


@router.get(
    "/{chat_id}/image",
    response_class=Response,
    responses={
        200: {"content": {kind: {} for kind in AVATAR_TYPES}},
        304: {"description": "the image the browser holds, named by If-None-Match, is the chat's still"},
        **READ_REFUSALS,
    },
)
async def get_image(
    request: Request,
    chat_id: ChatId,
    user: CurrentUser,
    if_none_match: Annotated[str | None, Header(description="the ETag of the image the browser holds")] = None,
) -> Response:
    """The group chat's image, for its members; 404 when it has none."""
    async with request.app.state.engine.begin() as conn:
        await chat_access(conn, user, chat_id)
        c = tables.chats
        sha256 = (await conn.execute(select(c.c.image_sha256).where(c.c.id == chat_id))).scalar_one()
    if sha256 is None:
        raise HTTPException(404, f"chat {chat_id} has no image: the web draws the faces of its members")
    etag = f'"{sha256}"'
    headers = {"Cache-Control": "private, max-age=86400", "ETag": etag}
    if _matches(if_none_match, etag):
        return Response(status_code=304, headers=headers)
    try:
        data = await asyncio.to_thread(fetch_bytes, blob_store(request), sha256, AVATAR_MAX_BYTES)
    except BlobStoreUnavailable:
        raise HTTPException(503, "the blob store did not answer; try again shortly") from None
    kind = media_type(data or b"")
    if kind is None:
        raise HTTPException(404, f"the image of chat {chat_id} is not in the blob store")
    headers |= {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'"}
    return Response(content=data, media_type=kind, headers=headers)
