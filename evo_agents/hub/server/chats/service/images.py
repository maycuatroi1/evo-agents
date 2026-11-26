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

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, chats
from evo_agents.hub.blobs import BlobStoreUnavailable
from evo_agents.hub.server import audit
from evo_agents.hub.server.agent_profiles import AVATAR_MAX_BYTES, AVATAR_TYPES, AvatarUpload, media_type
from evo_agents.hub.server.blobs import fetch_bytes, put_bytes
from evo_agents.hub.server.chats.models.access import ChatAccess
from evo_agents.hub.server.chats.models.images import IMAGE_KIND, UNAVAILABLE
from evo_agents.hub.server.chats.models.views import Chat
from evo_agents.hub.server.chats.repository import images as store
from evo_agents.hub.server.chats.repository.store import write_event
from evo_agents.hub.server.chats.service.access import chat_access
from evo_agents.hub.server.chats.service.views import chat_view
from evo_agents.hub.server.security import Principal


def group(chat: ChatAccess) -> ChatAccess:
    """``chat``, a group chat; 409 for a direct chat."""
    if chat.kind == chats.DIRECT:
        raise HTTPException(
            409,
            f"chat {chat.chat_id} is a direct chat, whose face is its agent's avatar: it takes no image of its own",
        )
    return chat


def image(body: AvatarUpload) -> tuple[bytes, str]:
    """(the bytes of ``body``'s image, their SHA-256); 413 over the size, 422 for what is no image."""
    data = base64.b64decode(body.data, validate=True)
    if len(data) > AVATAR_MAX_BYTES:
        raise HTTPException(413, f"the image is {len(data)} bytes, over the {AVATAR_MAX_BYTES} a chat's image may have")
    if media_type(data) is None:
        raise HTTPException(
            422, f"a chat's image is a {', '.join(AVATAR_TYPES.values())} image, and this one is none; nothing changed"
        )
    return data, hashlib.sha256(data).hexdigest()


async def store_image(blobs, sha256: str, data: bytes) -> None:
    """Put ``data``, of SHA-256 ``sha256``, in the blob store ``blobs``; 503 when it does not answer."""
    try:
        await asyncio.to_thread(put_bytes, blobs, sha256, data)
    except BlobStoreUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None


async def _change(conn: AsyncConnection, blobs, chat: ChatAccess, user: Principal, image, data: bytes) -> bool:
    """Make ``image`` ((SHA-256, size), or None) the chat's image, holding the blob lock exclusively: put its bytes
    back when their deletion was pending, record the old blob for deletion when nothing else refers to it, and write
    the event and the audit row. Whether it changed."""
    old = await store.image_of(conn, chat.chat_id)
    sha256, size = image if image is not None else (None, None)
    if old.image_sha256 == sha256:
        return False
    if sha256 is not None and await blob_gc.revive(conn, [sha256]):
        await asyncio.to_thread(put_bytes, blobs, sha256, data, replace=True)
    await store.set_image(conn, chat.chat_id, sha256, size)
    if old.image_sha256 is not None:
        await blob_gc.forget(conn, {old.image_sha256: (old.image_size, IMAGE_KIND)})
    await write_event(conn, chat.chat_id, chats.IMAGE_CHANGED, {"sha256": sha256, "by": user.login})
    target = f"chat:{chat.chat_id} image:{sha256[:16] if sha256 else 'none'}"
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.CHAT_IMAGE, target=target)
    return True


async def change_image(
    conn: AsyncConnection, blobs, chat_id: int, user: Principal, image, data: bytes = b""
) -> tuple[Chat, bool]:
    """Make ``image`` ((SHA-256, size), or None) the image of chat ``chat_id`` for ``user``, its bytes ``data`` in the
    blob store ``blobs`` (None to take it off): the chat as it reads now, and whether it changed."""
    await blob_gc.lock_exclusive(conn)  # before the chat's row, so its messages never wait for the lock
    chat = group(await chat_access(conn, user, chat_id, lock=True))
    changed = await _change(conn, blobs, chat, user, image, data)
    return await chat_view(conn, chat), changed


def matches(if_none_match: str | None, etag: str) -> bool:
    """Whether ``if_none_match`` names ``etag``."""
    if if_none_match is None:
        return False
    tags = [tag.strip().removeprefix("W/") for tag in if_none_match.split(",")]
    return "*" in tags or etag in tags


async def image_sha256(conn: AsyncConnection, user: Principal, chat_id: int) -> str:
    """The SHA-256 of the image of chat ``chat_id``, which ``user`` reads; 404 when it has none."""
    await chat_access(conn, user, chat_id)
    sha256 = (await store.image_of(conn, chat_id)).image_sha256
    if sha256 is None:
        raise HTTPException(404, f"chat {chat_id} has no image: the web draws the faces of its members")
    return sha256


async def image_bytes(blobs, chat_id: int, sha256: str) -> tuple[bytes, str]:
    """(the bytes of the image ``sha256`` of chat ``chat_id``, their media type); 503 when the blob store ``blobs``
    does not answer, 404 when it lacks them."""
    try:
        data = await asyncio.to_thread(fetch_bytes, blobs, sha256, AVATAR_MAX_BYTES)
    except BlobStoreUnavailable:
        raise HTTPException(503, "the blob store did not answer; try again shortly") from None
    kind = media_type(data or b"")
    if kind is None:
        raise HTTPException(404, f"the image of chat {chat_id} is not in the blob store")
    return data or b"", kind
