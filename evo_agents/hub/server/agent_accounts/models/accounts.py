"""The limits of the accounts of an agent and the path parameters of their routes; the shapes, which hold a worker's
name and an account's figures, are in ``evo_agents.hub.server.agent_accounts.service.shapes``."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Path

from evo_agents.hub import profiles
from evo_agents.hub.credentials import SECRET_NAME
from evo_agents.hub.server.errors import ErrorBody

MAX_ENV_VAR_CHARS = 200
MAX_WORKERS = 100
MAX_ACCOUNTS_PER_AGENT = 50
Handle = Annotated[str, Path(pattern=profiles.HANDLE_PATTERN, description="the agent's handle, @ left out")]
AccountName = Annotated[
    str, Path(pattern=f"^{SECRET_NAME.pattern}$", description="unique among the agent's live accounts")
]
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422, 503)}
