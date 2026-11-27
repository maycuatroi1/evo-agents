"""The shapes and limits of the audit trail and every user's tokens (``evo_agents.hub.server.admin_console``)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub.server.errors import ErrorBody

MAX_ID = 2**63 - 1  # bigint
MAX_LIMIT = 200
DEFAULT_LIMIT = 50
ACTION_NAME = r"^[a-z][a-z0-9_.-]{0,99}$"  # as audit's CHECK accepts it
CURSOR_HELP = "next_cursor of the previous page, with the same filters"
INVALID = {422: {"model": ErrorBody}}


class AuditRow(BaseModel):
    id: int
    at: datetime
    actor: str | None = Field(description="the login that acted; null for an action of the hub itself")
    token_id: int | None = Field(description="the token or web session the actor used")
    action: str
    target: str = Field(description="what the action named: a project, a login, a token id; never content")
    project: str | None = Field(description="the project the action happened in; null for one outside any project")


class AuditPage(BaseModel):
    items: list[AuditRow]
    next_cursor: str | None = Field(description="pass as cursor for the rows after these; null on the last page")


TokenState = Literal["active", "revoked", "expired"]
# What ``state`` of GET /v1/admin/tokens may ask for besides a token's own state, and ``any``.
TokenFilter = Literal["active", "revoked", "expired", "expiring", "unused", "any"]


class AdminToken(BaseModel):
    id: int
    login: str = Field(description="whose token it is")
    kind: Literal["machine", "web", "worker", "run"] = Field(
        description="run: the token of a run that runs as its owner (evo_agents.hub.server.run_access)"
    )
    host: str | None = Field(
        description="the machine a machine token or worker token was issued to, run:<id> for a run's; null for a web "
        "session"
    )
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime
    revoked_at: datetime | None
    state: TokenState
    current: bool = Field(description="the token this request came with")


class TokenPage(BaseModel):
    items: list[AdminToken]
    next_cursor: str | None = Field(description="pass as cursor for the tokens after these; null on the last page")
