"""The routes of the bridges of group chats: POST /v1/chats/{id}/bridges/code and DELETE
/v1/chats/{id}/bridges/{bridge_id} (``chats.service.bridges``).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from evo_agents.hub.server.chats.models.bridges import REFUSALS, ChatBridgeCode, ChatBridgeCodeIn
from evo_agents.hub.server.chats.models.members import ChatId
from evo_agents.hub.server.chats.models.views import Chat
from evo_agents.hub.server.chats.service.bridges import new_code, remove_bridge
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})


@router.post("/{chat_id}/bridges/code", response_model=ChatBridgeCode, status_code=201, responses=REFUSALS)
async def post_bridge_code(request: Request, chat_id: ChatId, body: ChatBridgeCodeIn, user: CurrentUser):
    """A one-time code that bridges the group chat to a group of ``kind``: the group's plugin takes it there within ten
    minutes. From a web session of an admin of every project of the chat's agents, each of which enables the kind's
    plugin with chats:read and chats:write."""
    async with request.app.state.engine.begin() as conn:
        code, expires_at = await new_code(conn, user, chat_id, body.kind)
    log.info("chat bridge code made", extra={"chat_id": chat_id, "kind": body.kind})
    return ChatBridgeCode(code=code, kind=body.kind, expires_at=expires_at)


@router.delete("/{chat_id}/bridges/{bridge_id}", response_model=Chat, responses=REFUSALS)
async def delete_bridge(request: Request, chat_id: ChatId, bridge_id: int, user: CurrentUser) -> Chat:
    """Take bridge ``bridge_id`` of the chat off: nothing goes to its group or comes from it any more. A chat admin
    does it from the web or the CLI, a hub admin from either."""
    async with request.app.state.engine.begin() as conn:
        return await remove_bridge(conn, user, chat_id, bridge_id)
