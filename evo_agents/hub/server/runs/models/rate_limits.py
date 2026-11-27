"""What a worker reports of an account's usage limit and what the hub answers (``evo_agents.hub.server.rate_limits``)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

MAX_WINDOWS = 16  # windows of usage one report carries
LIMITS = ("rejected", "unauthorized")  # the statuses that cool the account down and may move the run
Name = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")]


class UsageWindow(BaseModel):
    utilization: float | None = Field(default=None, ge=0, le=100)
    resets_at: datetime | None = None


class RateLimitReport(BaseModel):
    lease_id: int = Field(ge=1, description="the lease of the env secret the agent ran with, one the run holds")
    status: Literal["allowed", "allowed_warning", "rejected", "unauthorized"] = Field(
        description="what the CLI said: rejected when the account hit its usage limit, unauthorized when it failed to "
        "authenticate (its token refused), else its usage alone"
    )
    rate_limit_type: Name | None = Field(default=None, description="the limit, such as five_hour or seven_day")
    resets_at: datetime | None = Field(default=None, description="when the limit resets, when the CLI said")
    utilization: float | None = Field(default=None, ge=0, le=100, description="the share of the limit used")
    windows: dict[Name, UsageWindow] | None = Field(
        default=None, max_length=MAX_WINDOWS, description="the usage of each window the CLI listed"
    )


class RateLimitAnswer(BaseModel):
    action: Literal["none", "failover", "stop"] = Field(
        description="none: usage kept, go on; failover: ask for the run's leases again, the new account's value is "
        "in them, and go on in the same session; stop: end the run failed with cause rate_limit, or account_auth "
        "for unauthorized"
    )
    secret_id: int = Field(description="the account the run has now, by its secret's id")
    failovers: int = Field(description="the times the run moved to another account")
    cooldown_until: datetime | None = Field(
        default=None, description="with rejected or unauthorized: until when the account cools"
    )
    ready_at: datetime | None = Field(default=None, description="with stop: when an account is ready first")
    reason: str | None = Field(default=None, description="with stop: why the run cannot move")
