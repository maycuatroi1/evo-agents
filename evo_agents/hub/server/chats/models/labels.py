"""The level of a group chat's label (``chats.service.labels``): the body that sets it and its refusals."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from evo_agents.hub.server.errors import ErrorBody

REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
LEVEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


class ChatLabelIn(BaseModel):
    level: str = Field(
        pattern=LEVEL_PATTERN,
        description="the chat's level: on the ladder of every project of its agents, at or below each one's default",
    )
