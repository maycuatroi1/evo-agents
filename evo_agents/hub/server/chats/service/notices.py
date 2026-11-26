"""The notices of group chats (``evo_agents.hub.chats``): to a person someone mentioned in a chat (chat_mention) or
added to one (chat_added), never to the one who did it, through the outbox of ``notifications``. In a restricted chat
(``chat_access``) a notice names the chat and links to it, nothing more: no sender, no text. A notice is kept in the
project of one of the chat's agents, a restricted one first (``ChatAccess.notice_project``), so a member who loses the
grant on it stops getting it; one of a chat without agents is kept in no project.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats
from evo_agents.hub.server.chat_access import ChatAccess
from evo_agents.hub.server.notifications import notify

EXCERPT_CHARS = 1000  # of a message in the notice of a mention


async def notice_mention(conn: AsyncConnection, chat: ChatAccess, user_id: int, message_id: int, body: str) -> int:
    """Tell person ``user_id`` that ``chat``'s reader mentioned them in message ``message_id``."""
    if chat.restricted:
        title, text = f"You were mentioned in chat {chat.title}", None
    else:
        title = f"{chat.login} mentioned you in chat {chat.title}"
        text = body if len(body) <= EXCERPT_CHARS else body[: EXCERPT_CHARS - 3].rstrip() + "..."
    return await notify(
        conn,
        user_id=user_id,
        kind="notice",
        notice_kind=chats.NOTICE_MENTION,
        title=title,
        body=text,
        project_id=chat.notice_project,
        details={"chat_id": chat.chat_id, "message_id": message_id},
        link=chats.chat_link(chat.chat_id, message_id),
    )


async def notice_added(conn: AsyncConnection, chat: ChatAccess, user_id: int) -> int:
    """Tell person ``user_id`` that ``chat``'s reader added them to it."""
    title = f"You were added to chat {chat.title}" if chat.restricted else f"{chat.login} added you to {chat.title}"
    return await notify(
        conn,
        user_id=user_id,
        kind="notice",
        notice_kind=chats.NOTICE_ADDED,
        title=title,
        project_id=chat.notice_project,
        details={"chat_id": chat.chat_id},
        link=chats.chat_link(chat.chat_id),
    )
