"""The routes of who is in a group chat: PUT and DELETE /v1/chats/{id}/people/{login} and
/v1/chats/{id}/agents/{handle} (``chats.service.members``)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from evo_agents.hub.server.chats.models.members import REFUSALS, ChatId, HandlePath, LoginPath
from evo_agents.hub.server.chats.models.views import Chat
from evo_agents.hub.server.chats.service import members as service
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})


@router.put("/{chat_id}/people/{login}", response_model=Chat, responses=REFUSALS)
async def add_person(request: Request, chat_id: ChatId, login: LoginPath, user: CurrentUser) -> Chat:
    """Add a person to the chat; nothing changes when they are in it."""
    view, found, added = await service.add_person(request.app.state.engine, user, chat_id, login)
    log.info("chat person added", extra={"chat_id": chat_id, "login": found, "added": added})
    return view


@router.put("/{chat_id}/agents/{handle}", response_model=Chat, responses=REFUSALS)
async def add_agent(request: Request, chat_id: ChatId, handle: HandlePath, user: CurrentUser) -> Chat:
    """Add an agent to the chat (the writer role on its project); nothing changes when it is in it."""
    view, added = await service.add_agent(request.app.state.engine, user, chat_id, handle)
    log.info("chat agent added", extra={"chat_id": chat_id, "handle": handle, "added": added})
    return view


@router.delete("/{chat_id}/people/{login}", status_code=204, responses=REFUSALS)
async def remove_person(request: Request, chat_id: ChatId, login: LoginPath, user: CurrentUser) -> Response:
    """Leave the chat, or, for its creator, take a person out of it."""
    found = await service.remove_person(request.app.state.engine, user, chat_id, login)
    log.info("chat person removed", extra={"chat_id": chat_id, "login": found})
    return Response(status_code=204)


@router.delete("/{chat_id}/agents/{handle}", status_code=204, responses=REFUSALS)
async def remove_agent(request: Request, chat_id: ChatId, handle: HandlePath, user: CurrentUser) -> Response:
    """Take an agent out of the chat."""
    await service.remove_agent(request.app.state.engine, user, chat_id, handle)
    log.info("chat agent removed", extra={"chat_id": chat_id, "handle": handle})
    return Response(status_code=204)
