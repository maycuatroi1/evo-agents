"""What the chat home of the web lists (``chats.routes.home``): an agent a person may chat with."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ChatHomeAgent(BaseModel):
    handle: str
    project: str
    name: str
    description: str
    color: str
    icon_url: str | None = Field(description="GET of the avatar on this hub; null: draw the initial on the colour")
    starters: list[str] = Field(description="questions to offer before a first message, in order")
    is_default: bool = Field(description="the project's default agent, @<project>")
    chat_id: int | None = Field(description="your direct chat with the agent; null until one is opened")
    last_chatted_at: datetime | None = Field(
        description="when you last wrote in your direct chat with the agent; null when you never did"
    )
