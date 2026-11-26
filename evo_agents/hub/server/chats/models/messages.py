"""Group chats and their messages (``chats.service.chats``, ``chats.service.messages``): the bodies of the routes, what
they answer, and the texts of the hub's refusals."""

from __future__ import annotations

from typing import Annotated

from fastapi import Path
from pydantic import BaseModel, Field, field_validator, model_validator

from evo_agents.hub import chats, profiles
from evo_agents.hub.server.chats.models.members import LOGIN_PATTERN
from evo_agents.hub.server.runs.models import LINE, MAX_ID
from evo_agents.hub.server.runs.models.attachment_parts import AttachmentIds

PAGE = 50  # messages one page holds without limit
MAX_PAGE = 200
MessageId = Annotated[int, Path(ge=1, le=MAX_ID)]
Login = Annotated[str, Field(pattern=LOGIN_PATTERN)]
Handle = Annotated[str, Field(pattern=profiles.HANDLE_PATTERN)]
NOT_THERE = "chat {chat} has no message {id} that you can read"
NOT_IN_CHAT = "@{name} is not in this chat: add it to the chat, then mention it again"
NOT_IN_DIRECT = "@{name} is not in this direct chat, and the hub opened no group chat with it"


class ChatCreate(BaseModel):
    title: str = Field(min_length=1, max_length=chats.MAX_TITLE_CHARS, pattern=LINE)
    people: list[Login] = Field(
        default_factory=list, max_length=chats.MAX_PEOPLE - 1, description="logins; the creator is in it anyway"
    )
    agents: list[Handle] = Field(default_factory=list, max_length=chats.MAX_AGENTS, description="handles, @ left out")


class ChatMessageIn(BaseModel):
    body: str = Field(description=f"at most {chats.MAX_MESSAGE_BYTES} bytes of UTF-8; empty only with attachment_ids")
    reply_to: int | None = Field(None, ge=1, le=MAX_ID, description="the message it answers: it goes in its thread")
    attachment_ids: AttachmentIds = Field(default_factory=list)
    from_answer: int | None = Field(
        default=None,
        ge=1,
        le=MAX_ID,
        description=(
            "the output id of the interactive answer it is sent from: it goes in the thread of the message that "
            f"carries the answer, to the agent that wrote it; 1 to {chats.MAX_ANSWER_PROMPT_CHARS} characters of "
            f"text, at most {chats.ANSWER_PROMPTS} of a person in {chats.ANSWER_WINDOW} seconds (429)"
        ),
    )

    @model_validator(mode="after")
    def _bounded(self):
        if not self.body.strip() and not self.attachment_ids:
            raise ValueError("a message has some text")
        if len(self.body.encode()) > chats.MAX_MESSAGE_BYTES:
            raise ValueError(f"a message is at most {chats.MAX_MESSAGE_BYTES} bytes of UTF-8")
        if self.from_answer is not None and not 1 <= len(self.body.strip()) <= chats.MAX_ANSWER_PROMPT_CHARS:
            raise ValueError(f"a message from an interactive answer is 1 to {chats.MAX_ANSWER_PROMPT_CHARS} characters")
        return self


class ChatReactionIn(BaseModel):
    emoji: str = Field(min_length=1, max_length=chats.MAX_EMOJI_CHARS)

    @field_validator("emoji")
    @classmethod
    def _one_emoji(cls, value: str) -> str:
        problem = chats.emoji_problem(value)
        if problem is not None:
            raise ValueError(problem)
        return value


class ChatReadIn(BaseModel):
    seq: int = Field(ge=0, le=MAX_ID, description="the seq of the last event the member read")


class ChatRead(BaseModel):
    last_read_seq: int
    unread: int
