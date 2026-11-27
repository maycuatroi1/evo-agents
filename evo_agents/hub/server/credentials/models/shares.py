"""The shapes of the shares of secrets and accounts (``routes.shares``) and their limits."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Path
from pydantic import AwareDatetime, Field

from evo_agents.hub import org
from evo_agents.hub.credential_shares import GRANTEE_KINDS, MAX_BUDGET_USD, MAX_RUNNING, MAX_SHARE_PROJECTS
from evo_agents.hub.credentials import SECRET_NAME
from evo_agents.hub.server.base.models import LOGIN_NAME, PROJECT_NAME, StrictModel

NO_SHARE = "there is no share {share_id} of yours on this hub: see GET /v1/secrets/{{name}}/shares"
MAX_SHARE_ID = 2**63 - 1

SecretName = Annotated[str, Path(pattern=f"^{SECRET_NAME.pattern}$", description="one of the caller's secrets")]
ShareId = Annotated[int, Path(ge=1, le=MAX_SHARE_ID, description="the id of the share")]


class ShareWrite(StrictModel):
    grantee_kind: Literal[GRANTEE_KINDS] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="user: one member, named by login; unit: a unit of the org chart and every unit below it, named "
        "by unit; all: everyone on the hub"
    )
    login: str | None = Field(None, pattern=LOGIN_NAME, description="grantee_kind user only: the member's login")
    unit: str | None = Field(None, pattern=org.SLUG_PATTERN, description="grantee_kind unit only: the unit's slug")
    projects: list[Annotated[str, Field(pattern=PROJECT_NAME)]] = Field(
        min_length=1,
        max_length=MAX_SHARE_PROJECTS,
        description="the projects whose runs may use it, each one the secret is bound to",
    )
    level: Literal[org.LEVELS] | None = Field(None, description="the autonomy level a run using it is held to")  # pyright: ignore[reportInvalidTypeForm]
    budget_usd: float | None = Field(
        None, gt=0, le=MAX_BUDGET_USD, description="the most the runs using it may cost a month, in USD"
    )
    usage_ceiling_pct: int | None = Field(
        None, ge=1, le=100, description="no run takes the account while its usage is at this percent or above"
    )
    max_running: int | None = Field(None, ge=1, le=MAX_RUNNING, description="the most runs that may hold it at once")
    expires_at: AwareDatetime | None = Field(None, description="when the share ends; in the future")
    terms_ack: bool = Field(
        False,
        description="the owner acknowledges the terms warning, which sharing a personal subscription token needs",
    )
