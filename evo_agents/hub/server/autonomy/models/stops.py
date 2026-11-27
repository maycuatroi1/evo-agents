"""The shapes of a stop of autonomy S4 (``evo_agents.hub.server.autonomy.routes.stops``): what a stop covers, the body
that writes or lifts one, and the stop as written or lifted."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from evo_agents.hub import org
from evo_agents.hub.server.org_models import LOGIN_PATTERN

LOGIN, UNIT, ALL = "login", "unit", "all"
SCOPES = (LOGIN, UNIT, ALL)
MAX_REASON_CHARS = 1000
SLUG_PATTERN = org.SLUG_PATTERN


def scope_name(scope: str, login: str | None, slug: str | None) -> str:
    """What a stop covers, as the hub names it: ``login:<login>``, ``unit:<slug>`` or ``all``."""
    if scope == LOGIN:
        return f"login:{login}"
    return f"unit:{slug}" if scope == UNIT else ALL


class StopSet(BaseModel):
    """What to stop, or lift the stop of: a person, a unit and those below it, or the whole hub."""

    scope: Literal["login", "unit", "all"] = Field(description="login: a person; unit: a unit and below; all: the hub")
    login: Annotated[str | None, Field(pattern=LOGIN_PATTERN)] = Field(None, description="the person, for login")
    unit: Annotated[str | None, Field(pattern=SLUG_PATTERN)] = Field(None, description="the unit's slug, for unit")
    reason: str | None = Field(None, max_length=MAX_REASON_CHARS, description="why; a lift takes none")

    @model_validator(mode="after")
    def _named(self) -> StopSet:
        if (self.scope == LOGIN) != (self.login is not None) or (self.scope == UNIT) != (self.unit is not None):
            raise ValueError("scope login names a login, scope unit a unit, scope all neither")
        return self


class StopView(BaseModel):
    """A stop of autonomy, as written or lifted."""

    id: int
    scope: Literal["login", "unit", "all"]
    target: str = Field(description="login:<login>, unit:<slug> or all")
    reason: str | None
    stopped_by: str = Field(description="the login of who stopped")
    stopped_at: datetime
    lifted_by: str | None = Field(description="the login of who lifted it; null while it holds")
    lifted_at: datetime | None
    runs: list[int] = Field(
        default_factory=list, description="the runs this call cancelled or asked to stop; empty for a lift"
    )
    leases: int = Field(0, description="how many leases this call ended")
    told: list[str] = Field(default_factory=list, description="the logins told")
