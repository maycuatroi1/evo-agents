"""The group chats an isolated plugin posts in and reads (``routes.chats``), in the name of the admin who granted it
its permissions (``service.access.acting``), while the hub has the feature chats on (404 otherwise).

The chat is one its author is in (404 otherwise, 403 for one whose label they no longer clear, as for the member), that
holds an agent of the project, and whose every project of an agent grants the plugin the permission the route needs
(403 otherwise): a plugin never posts in a chat of people alone, or in one an agent of a project that did not grant it
takes part in. Posting needs its author to hold writer on the project (403), reading reader.
"""

from __future__ import annotations

from typing import cast

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import features, plugin_calls
from evo_agents.hub.server import hub_settings
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.chats.models.access import ChatAccess
from evo_agents.hub.server.chats.models.messages import ChatMessageIn
from evo_agents.hub.server.chats.repository.views import message_rows
from evo_agents.hub.server.chats.service.access import chat_access, label_reader
from evo_agents.hub.server.chats.service.messages import deliver_message, visible_message
from evo_agents.hub.server.chats.service.progress import by_message, chat_progress
from evo_agents.hub.server.chats.service.views import messages_view
from evo_agents.hub.server.plugin_api.models.access import Acting
from evo_agents.hub.server.plugin_api.models.chats import (
    FEATURE,
    NOT_GRANTED,
    NOT_OURS,
    READS,
    WRITES,
    PluginChatMessage,
    PluginMessageIn,
    PluginThread,
)
from evo_agents.hub.server.plugin_api.repository import chats as store
from evo_agents.hub.server.plugin_api.service.access import acting, granted
from evo_agents.hub.server.security import MACHINE, Principal
from evo_agents.hub.server.task_dispatch import plugin_of_request

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
    conn: AsyncConnection, plugin_id: int, project: str, chat_id: int, permission: str
) -> tuple[Acting, Principal, ChatAccess]:
    """(the plugin acting, its author, the author's access to the chat), once every check of the module passes."""
    enabled = await granted(conn, plugin_id, project, permission)
    who = await acting(conn, enabled, writes=permission == WRITES)
    author = _author(who)
    refusal = await hub_settings.refusal(conn, author, features.BY_ID[FEATURE], None)
    if refusal is not None:
        raise HTTPException(404, refusal)
    chat = await chat_access(conn, author, chat_id)
    await _chat_grants(conn, who, chat, permission)
    return who, author, chat


# What each route does


async def post(
    engine: AsyncEngine, request: Request, project: str, chat_id: int, body: PluginMessageIn
) -> tuple[str, PluginChatMessage]:
    """(the plugin's name, its message as its author reads it), posted in the name of the admin who granted it its
    permissions, in the thread of ``reply_to`` when given."""
    async with UnitOfWork(engine) as conn:
        plugin_id = await plugin_of_request(request, conn)
        who, author, chat = await _in_chat(conn, plugin_id, project, chat_id, WRITES)
        message = ChatMessageIn(body=body.body, reply_to=body.reply_to)
        message_id = await deliver_message(conn, chat, author, message, plugin=who.plugin)
        (row,) = await message_rows(conn, chat_id, [message_id])
        progress = by_message(await chat_progress(conn, author, chat_id))
        (view,) = await messages_view(conn, [row], await label_reader(conn, author.user_id, [row.label]), progress)
        return who.plugin, PluginChatMessage(**view.model_dump(), thread=view.thread_root_id or view.id)


async def thread(
    engine: AsyncEngine, request: Request, project: str, chat_id: int, of: int, before: int | None, limit: int
) -> PluginThread:
    """The latest ``limit`` messages of the thread of message ``of`` before ``before``, oldest first."""
    async with UnitOfWork(engine) as conn:
        plugin_id = await plugin_of_request(request, conn)
        _, author, chat = await _in_chat(conn, plugin_id, project, chat_id, READS)
        found = await visible_message(conn, chat, of)
        root = found.thread_root_id or found.id
        rows = await store.thread_rows(conn, chat_id, root, before, limit)
        page = list(reversed(rows[:limit]))
        messages = await messages_view(
            conn, page, await label_reader(conn, author.user_id, [row.label for row in page])
        )
        return PluginThread(chat_id=chat_id, thread=root, messages=messages, more=len(rows) > limit)


__all__ = ["post", "thread"]
