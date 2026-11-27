"""The routes of the level of a group chat's label: PUT and DELETE /v1/chats/{id}/label (``chats.service.labels``)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from evo_agents.hub.server.chats.models.labels import REFUSALS, ChatLabelIn
from evo_agents.hub.server.chats.models.members import ChatId
from evo_agents.hub.server.chats.models.views import Chat
from evo_agents.hub.server.chats.service.labels import set_level
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})


async def _set(request: Request, chat_id: int, user: Principal, level: str | None) -> Chat:
    view, changed = await set_level(request.app.state.engine, user, chat_id, level)
    log.info("chat label changed", extra={"chat_id": chat_id, "set": level is not None, "changed": changed})
    return view


@router.put("/{chat_id}/label", response_model=Chat, responses=REFUSALS)
async def put_label(request: Request, chat_id: ChatId, body: ChatLabelIn, user: CurrentUser) -> Chat:
    """Set the chat's level, at or below the default level of every project of its agents: a message sent from now
    on carries it. Needs the admin role on every one of those projects."""
    return await _set(request, chat_id, user, body.level)


@router.delete("/{chat_id}/label", response_model=Chat, responses=REFUSALS)
async def delete_label(request: Request, chat_id: ChatId, user: CurrentUser) -> Chat:
    """Take the chat's level off: its label is its projects' default again, for the messages sent from now on."""
    return await _set(request, chat_id, user, None)
