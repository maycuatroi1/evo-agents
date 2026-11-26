"""Who is in a group chat (``chats.service.members``): the path parameters of its member routes and their refusals."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Path

from evo_agents.hub import profiles
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import MAX_ID

LOGIN_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$"  # a GitHub login
ChatId = Annotated[int, Path(ge=1, le=MAX_ID)]
LoginPath = Annotated[str, Path(pattern=LOGIN_PATTERN, description="a person's login")]
HandlePath = Annotated[str, Path(pattern=profiles.HANDLE_PATTERN, description="an agent's handle, @ left out")]
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409)}

SHARE_HINT = (
    "A writer on the project whose own grant clears its label adds them, or its agent, and shares the project's part "
    "of the chat's label with them"
)
