"""The route of direct chats: PUT /v1/chats/direct/{handle} (``chats.service.direct``)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from evo_agents.hub.server.chats.models.members import REFUSALS, HandlePath
from evo_agents.hub.server.chats.models.views import Chat
from evo_agents.hub.server.chats.service.access import chat_access
from evo_agents.hub.server.chats.service.direct import direct_chat
from evo_agents.hub.server.chats.service.views import chat_view
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})


@router.put(
    "/direct/{handle}",
    response_model=Chat,
    responses={201: {"model": Chat, "description": "the direct chat, made now"}, **REFUSALS},
)
async def open_direct(request: Request, response: Response, handle: HandlePath, user: CurrentUser) -> Chat:
    """The caller's direct chat with agent ``handle``: the one there is (200), or one made now (201)."""
    async with request.app.state.engine.begin() as conn:
        chat_id, made = await direct_chat(conn, user, handle)
        view = await chat_view(conn, await chat_access(conn, user, chat_id))
    response.status_code = 201 if made else 200
    log.info("direct chat opened", extra={"chat_id": chat_id, "handle": handle, "made": made})
    return view
