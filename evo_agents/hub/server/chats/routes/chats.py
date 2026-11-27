"""The routes of group chats: GET and POST /v1/chats, GET /v1/chats/{id}, GET and POST .../messages, POST and DELETE
.../messages/{message}/reactions and POST .../read. ``chats.service.chats`` says what each does and
``chats.service.messages`` what a message hands the agents."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query, Request

from evo_agents.hub import chats
from evo_agents.hub.server.chats.models.members import REFUSALS, ChatId
from evo_agents.hub.server.chats.models.messages import (
    MAX_PAGE,
    PAGE,
    ChatCreate,
    ChatMessageIn,
    ChatReactionIn,
    ChatRead,
    ChatReadIn,
    MessageId,
)
from evo_agents.hub.server.chats.models.views import Chat, ChatPage, ChatSummary, GroupMessage
from evo_agents.hub.server.chats.service import chats as service
from evo_agents.hub.server.chats.service import summaries
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})


# The chats


@router.get("", response_model=list[ChatSummary])
async def list_chats(request: Request, user: CurrentUser) -> list[ChatSummary]:
    """The chats the caller is in, the latest active first, with the unread messages of others."""
    return await summaries.list_chats(request.app.state.engine, user)


@router.post("", status_code=201, response_model=Chat, responses=REFUSALS)
async def create_chat(request: Request, body: ChatCreate, user: CurrentUser) -> Chat:
    """Create a chat with the caller in it, and the people and agents given."""
    view, chat_id, people, agents = await service.create_chat(request.app.state.engine, user, body)
    log.info("chat created", extra={"chat_id": chat_id, "people": people, "agents": agents})
    return view


@router.get("/{chat_id}", response_model=Chat, responses=REFUSALS)
async def show_chat(request: Request, chat_id: ChatId, user: CurrentUser) -> Chat:
    """The chat with its people and agents."""
    return await service.show_chat(request.app.state.engine, user, chat_id)


# Messages


@router.get("/{chat_id}/messages", response_model=ChatPage, responses=REFUSALS)
async def list_messages(
    request: Request,
    chat_id: ChatId,
    user: CurrentUser,
    before: Annotated[int | None, Query(ge=1, le=MAX_ID, description="messages before this seq")] = None,
    thread: Annotated[int | None, Query(ge=1, le=MAX_ID, description="the first message of a thread")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = PAGE,
) -> ChatPage:
    """The chat and the latest ``limit`` messages before ``before``, or of one thread, oldest first."""
    return await service.list_messages(request.app.state.engine, user, chat_id, before, thread, limit)


@router.post("/{chat_id}/messages", status_code=201, response_model=GroupMessage, responses=REFUSALS)
async def send_message(request: Request, chat_id: ChatId, body: ChatMessageIn, user: CurrentUser) -> GroupMessage:
    """Send a message to the chat, in the thread of ``reply_to`` when given."""
    view = await service.send_message(request.app.state.engine, user, chat_id, body)
    log.info("chat message sent", extra={"chat_id": chat_id, "message_id": view.id, "mentions": len(view.mentions)})
    return view


# Reactions


@router.post("/{chat_id}/messages/{message_id}/reactions", response_model=GroupMessage, responses=REFUSALS)
async def react(
    request: Request, chat_id: ChatId, message_id: MessageId, body: ChatReactionIn, user: CurrentUser
) -> GroupMessage:
    """Put an emoji on a message; nothing changes when the caller put it already."""
    return await service.react(request.app.state.engine, user, chat_id, message_id, body)


@router.delete("/{chat_id}/messages/{message_id}/reactions", response_model=GroupMessage, responses=REFUSALS)
async def unreact(
    request: Request,
    chat_id: ChatId,
    message_id: MessageId,
    user: CurrentUser,
    emoji: Annotated[str, Query(min_length=1, max_length=chats.MAX_EMOJI_CHARS, description="the emoji to take off")],
) -> GroupMessage:
    """Take the caller's emoji off a message; nothing changes when it is not there."""
    return await service.unreact(request.app.state.engine, user, chat_id, message_id, emoji)


# Reading


@router.post("/{chat_id}/read", response_model=ChatRead, responses=REFUSALS)
async def read_chat(request: Request, chat_id: ChatId, body: ChatReadIn, user: CurrentUser) -> ChatRead:
    """Say how far the caller read: the seq of the last event they saw, at most the chat's last."""
    return await service.read_chat(request.app.state.engine, user, chat_id, body)
