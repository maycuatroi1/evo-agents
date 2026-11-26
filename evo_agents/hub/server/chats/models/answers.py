"""An interactive answer a person's message is sent from (``chats.service.answers``), and the texts of its refusals."""

from __future__ import annotations

from dataclasses import dataclass

NO_ANSWER = "chat {chat} has no interactive answer {id} that you can read"
OTHER_THREAD = "message {reply_to} is not in the thread of interactive answer {id}: send it without reply_to"
TOO_MANY = (
    "you sent {count} messages from interactive answers in the last {window} seconds, the most a person sends: "
    "wait a moment, or write in the chat"
)
NOT_IN_CHAT = "@{name} is not in this chat any more: add it to the chat, then ask again"
DELETED = "@{name} was deleted: it takes no new work"


@dataclass(frozen=True)
class Answer:
    """The interactive answer ``output_id``, carried by message ``message_id`` that agent ``profile_id`` wrote, in the
    thread of ``root_id``."""

    output_id: int
    message_id: int
    root_id: int
    profile_id: int
