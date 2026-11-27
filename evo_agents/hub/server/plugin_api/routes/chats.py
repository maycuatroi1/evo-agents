"""The group chats an isolated plugin posts in with its token (``docs/plugins.md``, section Messages of a plugin), in
the name of the admin who granted it its permissions (``service.chats``), while the hub has the feature chats on (404
otherwise):

- POST /v1/plugin-api/projects/{p}/chats/{chat}/messages, granted ``chats:write``: a message to the chat, in the
  thread of ``reply_to`` when given, through the code of a member's POST /v1/chats/{id}/messages
  (``chats.service.messages.deliver_message``): its mentions, the runs they hand agents (and the trace a run of an
  agent system's entry agent opens), the hub's answers to what they refuse, the notices and the audit row
  chat.message, whose target ends with plugin:<id>. The message comes ``via`` plugin (``chats.PLUGIN_VIA``). The
  answer is the message as its author reads it, with ``thread``, the first message of its thread.
- GET .../chats/{chat}/messages?thread=M, granted ``chats:read``: the messages of the thread message M is in, oldest
  first, the latest ``limit`` before ``before`` (a seq), as the author reads them.

A refusal is the hub's error body; nothing is written.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.chats.models.members import ChatId
from evo_agents.hub.server.chats.models.messages import MAX_PAGE, PAGE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plugin_api.models.chats import PluginChatMessage, PluginMessageIn, PluginThread
from evo_agents.hub.server.plugin_api.service import chats
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.task_dispatch import plugin_of_request

log = logging.getLogger(__name__)

REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (401, 403, 404, 422)}

router = APIRouter(prefix="/v1/plugin-api", tags=["plugin api"], responses={401: {"model": ErrorBody}})


@router.post(
    "/projects/{project}/chats/{chat_id}/messages",
    status_code=201,
    response_model=PluginChatMessage,
    responses=REFUSALS,
)
async def post_message(request: Request, project: ProjectName, chat_id: ChatId, body: PluginMessageIn):
    """Post a message to the chat in the name of the admin who granted the plugin its permissions, in the thread of
    ``reply_to`` when given, as their own message goes."""
    async with request.app.state.engine.begin() as conn:
        plugin_id = await plugin_of_request(request, conn)
        plugin, message = await chats.post(conn, plugin_id, project, chat_id, body)
    log.info(
        "plugin chat message sent",
        extra={"project": project, "plugin": plugin, "chat_id": chat_id, "message_id": message.id},
    )
    return message


@router.get("/projects/{project}/chats/{chat_id}/messages", response_model=PluginThread, responses=REFUSALS)
async def read_thread(
    request: Request,
    project: ProjectName,
    chat_id: ChatId,
    thread: Annotated[int, Query(ge=1, le=MAX_ID, description="a message of the thread, its first or a reply")],
    before: Annotated[int | None, Query(ge=1, le=MAX_ID, description="messages before this seq")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = PAGE,
) -> PluginThread:
    """The latest ``limit`` messages of the thread of message ``thread`` before ``before``, oldest first."""
    async with request.app.state.engine.begin() as conn:
        plugin_id = await plugin_of_request(request, conn)
        return await chats.thread(conn, plugin_id, project, chat_id, thread, before, limit)


__all__ = ["router"]
