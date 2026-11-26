"""Bridges of group chats to groups of a core plugin's channel (``chats.service.bridges``): the bodies of the code
route, the texts of the refusals and the form of an external id.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from evo_agents.hub.server.errors import ErrorBody

REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
KIND_PATTERN = r"^[a-z][a-z0-9_-]{0,62}$"
MAX_EXTERNAL_CHARS = 200  # migration 0165
UNLINKED_VIA = ("web", "cli", "bot", "admin")
READS, WRITES = "chats:read", "chats:write"
FROM_SESSION = (
    "a chat is bridged from a web session only, so a token that leaked cannot bridge one to a group of its own: sign "
    "in on the web and make the code there"
)
NOT_ADMIN = "bridging chat {chat} needs the admin role on every project of its agents: {names}"
UNKNOWN_KIND = "no core plugin of this hub contributes a channel of kind {kind!r}"
NOT_GRANTED = (
    "project {project} does not enable plugin {plugin} with chats:read and chats:write, which a bridge of kind {kind} "
    "needs: an admin of the project enables and grants it"
)
GROUP_TAKEN = "that group is bridged to a chat already: unbridge it there first"
NO_BRIDGE = "chat {chat} has no bridge {bridge}"


class ChatBridgeCodeIn(BaseModel):
    kind: str = Field(pattern=KIND_PATTERN, description="the kind of channel of the group, as a core plugin names it")


class ChatBridgeCode(BaseModel):
    code: str = Field(description="the one-time code the group's plugin takes, shown once")
    kind: str
    expires_at: datetime = Field(description="ten minutes after it was made")


def external_id(value) -> str:
    """``value``, the id of a group or of a message there, as the hub keeps it: 1 to MAX_EXTERNAL_CHARS characters."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("an external id is a string or an integer")
    text = str(value)
    if not 1 <= len(text) <= MAX_EXTERNAL_CHARS:
        raise ValueError(f"an external id has 1 to {MAX_EXTERNAL_CHARS} characters")
    return text
