"""The shapes and refusal texts of the group chats a plugin posts in and reads (``routes.chats``)."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from evo_agents.hub import chats
from evo_agents.hub.server.base.models import StrictModel
from evo_agents.hub.server.chats.models.views import GroupMessage
from evo_agents.hub.server.runs.models import MAX_ID

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


class PluginMessageIn(StrictModel):
    """A message of the plugin: its text, and the message it answers."""

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


__all__ = [
    "FEATURE",
    "NOT_GRANTED",
    "NOT_OURS",
    "READS",
    "WRITES",
    "PluginChatMessage",
    "PluginMessageIn",
    "PluginThread",
]
