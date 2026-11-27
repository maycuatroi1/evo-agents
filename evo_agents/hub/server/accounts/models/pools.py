"""The shapes of the account pools of a project and the path parameters of their routes
(``evo_agents.hub.server.accounts.service.pools`` says what each route does)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any

from fastapi import Path
from pydantic import BaseModel, ConfigDict, Field

from evo_agents.hub.accounts import (
    DEFAULT_FIVE_HOUR_CEILING_PCT,
    DEFAULT_HARD_RESERVE_PCT,
    DEFAULT_STRATEGY,
    MAX_PCT,
    Strategy,
)
from evo_agents.hub.credentials import PROVIDER_KIND
from evo_agents.hub.server.errors import ErrorBody

POOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
MEMBER_KINDS = ("env", PROVIDER_KIND)
PoolName = Annotated[str, Path(pattern=POOL_NAME.pattern, description="the pool's name, 1 to 40 characters")]
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
NEEDS_POOL = (
    "agent @{handle} has no account pool, so a run of it has no account to use: ask an admin of "
    "project {project} to attach one (`evo-agents hub agent account-pool {handle} POOL`)"
)


class PoolWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=POOL_NAME.pattern, description="1 to 40 characters, unique in the project in any case")
    strategy: Strategy = Field(DEFAULT_STRATEGY, description="how a run picks among the pool's accounts")
    hard_reserve_pct: int = Field(
        DEFAULT_HARD_RESERVE_PCT,
        ge=0,
        le=MAX_PCT,
        description="quota-pacing: the share of a weekly window kept back while another account has more",
    )
    five_hour_ceiling_pct: int = Field(
        DEFAULT_FIVE_HOUR_CEILING_PCT,
        ge=1,
        le=MAX_PCT,
        description="quota-pacing: the five-hour use above which an account waits while a cooler one is ready",
    )


class PoolPolicy(BaseModel):
    """What PATCH .../account-pools/{pool} changes; a field left out stays as it is."""

    model_config = ConfigDict(extra="forbid")

    strategy: Strategy | None = None
    hard_reserve_pct: int | None = Field(None, ge=0, le=MAX_PCT)
    five_hour_ceiling_pct: int | None = Field(None, ge=1, le=MAX_PCT)


class AccountAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, description="the name of the account: one of yours, or an agent's")
    agent: str | None = Field(
        None, description="the handle of the agent of the project that owns the account; null: your own account"
    )


class PoolAccount(BaseModel):
    id: int = Field(description="the id of its secret, which audit lines name it by")
    name: str
    kind: str = Field(description="env or provider")
    env_var: str | None = Field(description="the variable an env account sets; null for a provider account")
    provider: str = Field(description="who it signs in to: claude, anthropic, openai ... or a catalog id")
    category: str = Field(description="model: the runtime's own sign-in or a provider account; service: any other")
    owner: str | None = Field(description="the member who owns it; null for an agent's account")
    agent: str | None = Field(description="the agent that owns it; null for a member's account")
    paused: bool
    cooldown_until: datetime | None
    expires_at: datetime | None
    added_by: str
    added_at: datetime
    last_usage: dict | None = Field(
        None, description="its last usage: windows five_hour and seven_day (a weekly budget's among them), 0 to 1"
    )


class AccountPool(BaseModel):
    id: int
    project: str
    name: str
    strategy: str = Field(DEFAULT_STRATEGY, description="how a run picks among its accounts")
    hard_reserve_pct: int = DEFAULT_HARD_RESERVE_PCT
    five_hour_ceiling_pct: int = DEFAULT_FIVE_HOUR_CEILING_PCT
    created_by: str
    created_at: datetime
    accounts: list[PoolAccount]
    agents: list[str] = Field(description="the handles of the agents that use it")
