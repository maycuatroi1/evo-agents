"""The chat of an author run (``chats.service.author``): its link, the bodies the worker posts and the chat the owner
reads."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from evo_agents.hub import author, runs
from evo_agents.hub.server.runs.models.attachment_parts import SentFile

WAITING_TITLE = "Author run #{id} waits for your reply"


def chat_link(project: str, run_id: int) -> str:
    """The web page of an author run's chat, as its notice links to it."""
    return f"/p/{project}/runs/{run_id}?tab=chat"


class ChatPost(BaseModel):
    text: str = Field(
        min_length=1, max_length=author.MAX_CHAT_BYTES, description="the agent's last message of a turn, at most 16 KiB"
    )

    @model_validator(mode="after")
    def _bounded(self):
        if not self.text.strip():
            raise ValueError("a message of the chat needs some text")
        if len(self.text.encode()) > author.MAX_CHAT_BYTES:
            raise ValueError(f"a message of the chat is at most {author.MAX_CHAT_BYTES} bytes of UTF-8")
        return self


class ChatPosted(BaseModel):
    run_id: int
    seq: int = Field(description="its event in the run's log")


class ChatMessage(BaseModel):
    run_id: int = Field(description="the run whose log holds it: the run read, or one it resumes or that resumes it")
    seq: int = Field(description="its event in that run's log")
    author: Literal["agent", "owner"]
    login: str | None = Field(description="the owner's login for the owner's message; null for the agent's")
    text: str
    at: datetime
    attachments: list[SentFile] = Field(
        default_factory=list,
        description="the files of the owner's message, whose bytes GET /v1/projects/{p}/runs/{id}/attachments/{aid}"
        "/content serves",
    )


class RunChat(BaseModel):
    run_id: int = Field(description="the run of the chat that takes the next message: the latest that resumes it")
    state: Literal[runs.RUN_STATES] = Field(description="that run's state")  # pyright: ignore[reportInvalidTypeForm]
    status: Literal[author.CHAT_STATUSES] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="working: the agent works on its turn; waiting: it waits for your reply (waiting or parked); "
        "ended: the run ended"
    )
    owner: str = Field(description="the login of the run's owner, the one member who replies")
    plan_id: str | None = Field(description="the plan the run wrote or revises; null before its first put")
    plan_revision: int | None = Field(description="that plan's revision the run last wrote, or was dispatched on")
    messages: list[ChatMessage] = Field(description="oldest first")
    more: bool = Field(
        description=f"older messages were left out: the chat shows its {author.MAX_CHAT_MESSAGES} latest"
    )
