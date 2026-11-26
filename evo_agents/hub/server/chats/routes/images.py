"""The routes of the image of a group chat: PUT, DELETE and GET /v1/chats/{id}/image (``chats.service.images``)."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response

from evo_agents.hub.blobs import BlobStoreUnavailable
from evo_agents.hub.server.agent_profiles import AVATAR_TYPES, AvatarUpload
from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.chats.models.images import DROP_REFUSALS, READ_REFUSALS, SET_REFUSALS, UNAVAILABLE
from evo_agents.hub.server.chats.models.members import ChatId
from evo_agents.hub.server.chats.models.views import Chat
from evo_agents.hub.server.chats.service import images
from evo_agents.hub.server.chats.service.access import chat_access
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})


async def _set(request: Request, chat_id: int, user: Principal, image, data: bytes = b"") -> Chat:
    blobs = blob_store(request) if image is not None else None
    try:
        async with request.app.state.engine.begin() as conn:
            view, changed = await images.change_image(conn, blobs, chat_id, user, image, data)
    except BlobStoreUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None
    log.info("chat image changed", extra={"chat_id": chat_id, "set": image is not None, "changed": changed})
    return view


@router.put("/{chat_id}/image", response_model=Chat, responses=SET_REFUSALS)
async def put_image(request: Request, chat_id: ChatId, body: AvatarUpload, user: CurrentUser) -> Chat:
    """Set the group chat's image, a PNG, JPEG or WebP image: its members see it in their list and its header."""
    async with request.app.state.engine.begin() as conn:  # the refusals of the caller before the bytes are stored
        images.group(await chat_access(conn, user, chat_id))
    data, sha256 = images.image(body)
    await images.store_image(blob_store(request), sha256, data)
    return await _set(request, chat_id, user, (sha256, len(data)), data)


@router.delete("/{chat_id}/image", response_model=Chat, responses=DROP_REFUSALS)
async def delete_image(request: Request, chat_id: ChatId, user: CurrentUser) -> Chat:
    """Take the group chat's image off: the web draws the faces of its members again."""
    return await _set(request, chat_id, user, None)


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
        sha256 = await images.image_sha256(conn, user, chat_id)
    etag = f'"{sha256}"'
    headers = {"Cache-Control": "private, max-age=86400", "ETag": etag}
    if images.matches(if_none_match, etag):
        return Response(status_code=304, headers=headers)
    data, kind = await images.image_bytes(blob_store(request), chat_id, sha256)
    headers |= {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'"}
    return Response(content=data, media_type=kind, headers=headers)
