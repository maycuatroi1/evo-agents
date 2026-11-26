"""Whom a person may add to a group chat (``chats.routes.people``): a candidate and why they could not be in it."""

from __future__ import annotations

from pydantic import BaseModel, Field

MAX_CANDIDATES = 500


class ChatCandidate(BaseModel):
    login: str
    avatar_url: str | None = Field(description="their GitHub avatar; null for a login that never signed in")
    why: str | None = Field(
        description="why they could not be in a chat with the agents given, as the hub's refusal says it; null when "
        "they could"
    )
