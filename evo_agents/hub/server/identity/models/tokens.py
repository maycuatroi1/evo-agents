"""The shape of one of the caller's tokens (``evo_agents.hub.server.tokens``)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

MAX_ID = 2**63 - 1  # bigint


class TokenRow(BaseModel):
    id: int
    kind: Literal["machine", "web", "worker", "run"] = Field(
        description="run: the token of a run of yours that runs as you (evo_agents.hub.server.run_access)"
    )
    host: str | None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime
    revoked_at: datetime | None
    state: Literal["active", "revoked", "expired"]
    current: bool  # the token this request came with
