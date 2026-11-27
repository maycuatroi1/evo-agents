"""The shapes of what a member reads of their accounts at work: the figures of an account and the accounts of a run
(``evo_agents.hub.server.accounts.service.views`` says what each holds)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from evo_agents.hub.accounts import CHOICES
from evo_agents.hub.gateway_runs import GATEWAY
from evo_agents.hub.server.errors import ErrorBody

REFUSALS: dict[int | str, dict[str, Any]] = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}
DAY, WEEK = timedelta(days=1), timedelta(days=7)
TAKEN_BY = ("secret", GATEWAY)  # the leases that say a run took an account: of its value, or through the gateway
MAX_SWITCHES = 50  # usage limits of one run the route lists, the latest


class AccountUsageWindow(BaseModel):
    utilization: float | None = None
    resets_at: datetime | None = None


class AccountUsage(BaseModel):
    """What the agent's CLI last said of an account's usage, in a rate_limit_event."""

    model_config = ConfigDict(extra="ignore")

    status: Literal["allowed", "allowed_warning", "rejected"]
    rate_limit_type: str | None = None
    resets_at: datetime | None = None
    utilization: float | None = None
    windows: dict[str, AccountUsageWindow] | None = None


class HeldBy(BaseModel):
    run_id: int
    project: str
    state: str


class RunAccountEvent(BaseModel):
    at: datetime
    action: Literal["failover", "stop"] = Field(description="the run moved to another account, or it stopped")
    account: str | None = Field(description="the account that hit its usage limit, by name")
    to: str | None = Field(description="with failover: the account the run went on with")
    cooldown_until: datetime | None = Field(description="until when the account cools down")


class RunAccount(BaseModel):
    env_var: str
    account: str | None = Field(description="the account the run has now, by name")
    how: Literal[CHOICES] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="picked by hand, bound to the worker, by the charter, the pool, cooling, shared by another member"
    )
    failovers: int = Field(description="the times the run moved to another account")
    chosen_at: datetime
    events: list[RunAccountEvent] = Field(description="each usage limit the run hit with it, oldest first")


class PrincipalCost(BaseModel):
    principal: str | None = Field(description="the member the runs were made for, by login; null for one now gone")
    runs_24h: int
    runs_7d: int
    runs_month: int = Field(description="the runs that took it since the month began, UTC")
    cost_24h_usd: float
    cost_7d_usd: float
    cost_month_usd: float = Field(description="what those runs cost since the month began, in USD, an estimate")


@dataclass
class Figures:
    last_usage: AccountUsage | None = None
    last_usage_at: datetime | None = None
    runs_24h: int = 0
    runs_7d: int = 0
    cost_24h_usd: float = 0.0
    cost_7d_usd: float = 0.0
    held_by: list[HeldBy] = field(default_factory=list)
    cost_by_principal: list[PrincipalCost] = field(default_factory=list)
