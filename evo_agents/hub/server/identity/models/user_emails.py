"""The shapes and limits of a member's emails (``evo_agents.hub.server.user_emails``)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

MAX_ID = 2**63 - 1  # bigint
MAX_BODY_CHARS = 1000  # an address with spaces around it still fits; email_problem holds the real bound


class UserEmail(BaseModel):
    id: int
    email: str  # as it was written
    login: str  # the member it belongs to
    created_at: datetime


class EmailAdd(BaseModel):
    email: str = Field(min_length=1, max_length=MAX_BODY_CHARS, description="one address; case does not matter")
