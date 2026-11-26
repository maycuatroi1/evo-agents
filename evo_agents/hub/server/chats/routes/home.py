"""The route of the chat home of the web: GET /v1/me/chat-agents (``chats.service.home``)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.chats.models.home import ChatHomeAgent
from evo_agents.hub.server.chats.service.home import chat_agents
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/me", tags=["chats"], responses={401: {"model": ErrorBody}})


@router.get("/chat-agents", response_model=list[ChatHomeAgent])
async def list_chat_agents(request: Request, user: CurrentUser) -> list[ChatHomeAgent]:
    """The agents you may chat with on any project you hold a grant on, the ones you wrote to last first."""
    async with request.app.state.engine.begin() as conn:
        return await chat_agents(conn, user)
