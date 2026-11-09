"""The group chats an isolated plugin posts in with its token (``docs/plugins.md``, section Messages of a plugin), in
the name of the admin who granted it its permissions (``plugin_api_access.acting``), while the hub has the feature
chats on (404 otherwise):

- POST /v1/plugin-api/projects/{p}/chats/{chat}/messages, granted ``chats:write``: a message to the chat, in the
  thread of ``reply_to`` when given, through the code of a member's POST /v1/chats/{id}/messages
  (``chats.deliver_message``): its mentions, the runs they hand agents (and the trace a run of an agent system's entry
  agent opens), the hub's answers to what they refuse, the notices and the audit row chat.message, whose target ends
  with plugin:<id>. The message comes ``via`` plugin (``chats.PLUGIN_VIA``). The answer is the message as its author
  reads it, with ``thread``, the first message of its thread.
- GET .../chats/{chat}/messages?thread=M, granted ``chats:read``: the messages of the thread message M is in, oldest
  first, the latest ``limit`` before ``before`` (a seq), as the author reads them.

The chat is one its author is in (404 otherwise, 403 for one whose label they no longer clear, as for the member), that
holds an agent of project {p}, and whose every project of an agent grants the plugin the permission the route needs
(403 otherwise): a plugin never posts in a chat of people alone, or in one an agent of a project that did not grant it
takes part in. Posting needs its author to hold writer on {p} (403), reading reader. A refusal is the hub's error body;
nothing is written.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, cast

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, features, plugin_calls, tables
from evo_agents.hub.server import hub_settings
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.chat_access import ChatAccess, chat_access, label_reader
from evo_agents.hub.server.chat_members import ChatId
from evo_agents.hub.server.chat_progress import by_message, chat_progress
from evo_agents.hub.server.chat_views import GroupMessage, message_rows, message_select, messages_view
from evo_agents.hub.server.chats import MAX_PAGE, PAGE, ChatMessageIn, deliver_message, visible_message
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plugin_api_access import Acting, acting, granted
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.security import MACHINE, Principal
from evo_agents.hub.server.task_dispatch import plugin_of_request

log = logging.getLogger(__name__)

READS, WRITES = "chats:read", "chats:write"
FEATURE = "chats"
NOT_OURS = (
    "chat {chat} holds no agent of project {project}: plugin {plugin} posts in and reads the chats that hold an agent "
    "of the project it acts in"
)
NOT_GRANTED = (
    "chat {chat} holds an agent of project {project}, which does not grant plugin {plugin} {permission}: an admin of "
    "that project enables the plugin with it"
)
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (401, 403, 404, 422)}

router = APIRouter(prefix="/v1/plugin-api", tags=["plugin api"], responses={401: {"model": ErrorBody}})


class PluginMessageIn(BaseModel):
    """A message of the plugin: its text, and the message it answers."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(
        description=f"some text, at most {chats.MAX_MESSAGE_BYTES} bytes of UTF-8, whose mentions read as a member's"
    )
    reply_to: int | None = Field(
        None, ge=1, le=MAX_ID, description="the message it answers, of the chat: it goes in that message's thread"
    )

    @model_validator(mode="after")
    def _bounded(self):
        if not self.body.strip():
            raise ValueError("a message has some text")
        if len(self.body.encode()) > chats.MAX_MESSAGE_BYTES:
            raise ValueError(f"a message is at most {chats.MAX_MESSAGE_BYTES} bytes of UTF-8")
        return self


class PluginChatMessage(GroupMessage):
    """A message the plugin posted, as its author reads it, with the first message of its thread."""

    thread: int = Field(
        description="the first message of its thread, its own id when it starts one: what reply_to and ?thread= take"
    )


class PluginThread(BaseModel):
    """A page of the messages of one thread, oldest first."""

    chat_id: int
    thread: int = Field(description="the first message of the thread")
    messages: list[GroupMessage]
    more: bool = Field(description="the thread has older messages: ask again with before, the seq of the first here")


# Who acts, and in which chat


def _author(who: Acting) -> Principal:
    """The admin who granted the plugin its permissions as the chat's checks take a member: a token of no kind of the
    web, never a hub admin."""
    return Principal(
        user_id=who.user_id, login=who.login, admin=False, token_id=cast(int, None), kind=MACHINE, token_hash=""
    )


async def _chat_grants(conn: AsyncConnection, who: Acting, chat: ChatAccess, permission: str) -> None:
    """403 unless the chat holds an agent of the plugin's project and every project of its agents grants the plugin
    ``permission``."""
    if who.access.project_id not in {project.id for project in chat.projects}:
        raise HTTPException(403, NOT_OURS.format(chat=chat.chat_id, project=who.access.name, plugin=who.plugin))
    for project in chat.projects:
        enabled = await plugin_calls.enabled_in(conn, who.plugin, project.id)
        if enabled is None or permission not in plugin_calls.effective_grant(enabled):
            refusal = NOT_GRANTED.format(
                chat=chat.chat_id, project=project.name, plugin=who.plugin, permission=permission
            )
            raise HTTPException(403, refusal)


async def _in_chat(
    request: Request, conn: AsyncConnection, project: str, chat_id: int, permission: str
) -> tuple[Acting, Principal, ChatAccess]:
    """(the plugin acting, its author, the author's access to the chat), once every check of the module passes."""
    plugin_id = await plugin_of_request(request, conn)
    enabled = await granted(conn, plugin_id, project, permission)
    who = await acting(conn, enabled, writes=permission == WRITES)
    author = _author(who)
    refusal = await hub_settings.refusal(conn, author, features.BY_ID[FEATURE], None)
    if refusal is not None:
        raise HTTPException(404, refusal)
    chat = await chat_access(conn, author, chat_id)
    await _chat_grants(conn, who, chat, permission)
    return who, author, chat


# The routes


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
        who, author, chat = await _in_chat(request, conn, project, chat_id, WRITES)
        message = ChatMessageIn(body=body.body, reply_to=body.reply_to)
        message_id = await deliver_message(conn, chat, author, message, plugin=who.plugin)
        (row,) = await message_rows(conn, chat_id, [message_id])
        progress = by_message(await chat_progress(conn, author, chat_id))
        (view,) = await messages_view(conn, [row], await label_reader(conn, author.user_id, [row.label]), progress)
    log.info(
        "plugin chat message sent",
        extra={"project": project, "plugin": who.plugin, "chat_id": chat_id, "message_id": message_id},
    )
    return PluginChatMessage(**view.model_dump(), thread=view.thread_root_id or view.id)


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
    cm = tables.chat_messages
    async with request.app.state.engine.begin() as conn:
        _, author, chat = await _in_chat(request, conn, project, chat_id, READS)
        found = await visible_message(conn, chat, thread)
        root = found.thread_root_id or found.id
        query = message_select().where(cm.c.chat_id == chat_id, or_(cm.c.id == root, cm.c.thread_root_id == root))
        if before is not None:
            query = query.where(cm.c.seq < before)
        rows = (await conn.execute(query.order_by(cm.c.seq.desc()).limit(limit + 1))).all()
        page = list(reversed(rows[:limit]))
        messages = await messages_view(
            conn, page, await label_reader(conn, author.user_id, [row.label for row in page])
        )
    return PluginThread(chat_id=chat_id, thread=root, messages=messages, more=len(rows) > limit)


__all__ = ["router"]
