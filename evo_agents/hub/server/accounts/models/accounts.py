"""The shapes of a member's accounts and their policies, and the path parameters and limits of their routes
(``evo_agents.hub.server.accounts.service.accounts`` says what each route does)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Path, Query
from pydantic import AwareDatetime, BaseModel, Field

from evo_agents.hub.account_providers import Category, Provider
from evo_agents.hub.accounts import (
    ACCOUNT_STATES,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_FIVE_HOUR_CEILING_PCT,
    DEFAULT_HARD_RESERVE_PCT,
    DEFAULT_MAX_FAILOVERS,
    DEFAULT_STRATEGY,
    MAX_COOLDOWN_SECONDS,
    MAX_FAILOVERS,
    MAX_PCT,
    MAX_PRIORITY,
    MAX_WEIGHT,
    MIN_COOLDOWN_SECONDS,
    STRATEGIES,
)
from evo_agents.hub.credentials import SECRET_NAME
from evo_agents.hub.server.accounts.models.views import AccountUsage, HeldBy, PrincipalCost
from evo_agents.hub.server.base.models import LOGIN_NAME, StrictModel
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.share_views import CredentialShare

NO_ACCOUNT = "you have no env secret named {name} on this hub: see GET /v1/accounts"
AccountName = Annotated[str, Path(pattern=f"^{SECRET_NAME.pattern}$", description="one of the caller's env secrets")]
OwnerLogin = Annotated[
    str | None,
    Query(pattern=LOGIN_NAME, description="the member who shared the account with the caller; omit for one's own"),
]
REFUSALS: dict[int | str, dict[str, Any]] = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}
FAILING = ("error", "unavailable", "expired", "banned", "credits_exhausted")  # testStatus of a connection that fails
MAX_BUDGET_USD = 1_000_000
REASON, MAX_REASON = re.compile(r"[^A-Za-z0-9_.:-]+"), 80  # a reason as shown: a code, never the provider's text


class GatewayAccount(BaseModel):
    """What a provider account calls, through the model gateway (``evo_agents.hub.gateway_runs``)."""

    provider: str = Field(description="the id of its entry in the catalog")
    prefix: str | None = Field(description="what its models are named with, <prefix>/<model>; null when not served")
    base_url_host: str | None = Field(description="the host of the endpoint it calls")
    api_format: str
    models: int = Field(description="how many models it serves")
    status: Literal["ok", "cooling", "failing", "unknown"] = Field(
        description="what the gateway last said of it: ok, cooling after a rate limit, failing, or unknown before any"
    )
    reason: str | None = Field(description="the gateway's error code or type when cooling or failing; never its text")


class Account(BaseModel):
    """One of the caller's env secrets or provider accounts as an account: everything but its value or key."""

    name: str
    env_var: str = Field(description="the variable it sets")
    provider: Provider = Field(description="who it signs in to, read from its variable (``account_providers``)")
    category: Category = Field(description="model: the runtime's own sign-in, whose runs cost; service: any other")
    projects: list[str] = Field(description="the projects whose runs may take it")
    workers: list[str] = Field(description="the workers it is bound to; empty for any worker of its owner")
    pools: list[str] = Field([], description="the worker pools it is bound to, each node of each")
    auth: Literal["token", "oauth"] = Field(
        description="oauth: it signed in with OAuth and the hub keeps its refresh token sealed; token: a token as given"
    )
    weight: int = Field(description=f"0 to {MAX_WEIGHT}: its share in a weighted-round-robin pool; 0 leaves it")
    priority: int = Field(description=f"0 to {MAX_PRIORITY}: the lower, the sooner a fill-first pool takes it")
    paused_at: datetime | None = Field(description="when its owner paused it; null while it may be taken")
    cooldown_until: datetime | None = Field(description="until when it cools down after a usage limit")
    cooldown_reason: str | None = Field(description="why it cools down")
    last_leased_at: datetime | None = Field(description="when a run last took it")
    expires_at: datetime | None
    state: Literal[ACCOUNT_STATES] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="ready, cooling, paused, expired or needs-relogin (an OAuth account whose refresh token was "
        "refused: run oauth-login again), now"
    )
    last_usage: AccountUsage | None = Field(description="what a run's CLI last said of its usage; null before any")
    last_usage_at: datetime | None = Field(description="when a run reported that usage")
    runs_24h: int = Field(description="the runs that took it in the last 24 hours")
    runs_7d: int = Field(description="the runs that took it in the last 7 days")
    cost_24h_usd: float | None = Field(
        description="what those runs cost in the last 24 hours, in USD, from their usage; null for a service account"
    )
    cost_7d_usd: float | None = Field(
        description="what those runs cost in the last 7 days, in USD, an estimate; null for a service account"
    )
    held_by: list[HeldBy] = Field(description="the runs a worker holds now with it")
    shares: list[CredentialShare] = Field(description="its shares not revoked, newest first")
    cost_by_principal: list[PrincipalCost] = Field(
        description="its runs and their cost by the member each was made for; for a member it was shared with, theirs"
    )
    gateway: GatewayAccount | None = Field(
        None, description="a provider account's endpoint and what the model gateway last said of it; null for a secret"
    )
    weekly_budget_usd: float | None = Field(
        None, description="the USD its owner gives it a week, turned into its seven_day window; null for none"
    )
    weekly_reset_at: datetime | None = Field(
        None, description="an instant its budget's weeks start from, every 7 days; null: Monday 00:00 UTC"
    )


class SharedAccount(Account):
    """An env account another member shares with the caller, as GET /v1/accounts/{name}?owner= gives it: its figures
    are the caller's own runs alone, its shares those that lend it to the caller, and its projects those of these
    shares its owner binds it to."""

    owner: str = Field(description="the login of the member who owns it and shares it")
    pick: str = Field(description="how ``account`` of a dispatch names it: <owner>/<name>")


class AccountChange(StrictModel):
    weight: int | None = Field(None, ge=0, le=MAX_WEIGHT, description="its share in a weighted pool; 0 leaves it")
    priority: int | None = Field(None, ge=0, le=MAX_PRIORITY, description="the lower, the sooner in a fill-first pool")
    paused: bool | None = Field(None, description="true pauses it, so no new run takes it; false resumes it")
    weekly_budget_usd: float | None = Field(
        None, gt=0, le=MAX_BUDGET_USD, description="the USD it may spend a week, for a model account; null drops it"
    )
    weekly_reset_at: AwareDatetime | None = Field(
        None, description="an instant its budget's weeks start from, every 7 days; null: Monday 00:00 UTC"
    )


class AccountPolicyWrite(StrictModel):
    strategy: Literal[STRATEGIES] = Field(DEFAULT_STRATEGY, description="how a run picks among the accounts")  # pyright: ignore[reportInvalidTypeForm]
    cooldown_seconds: int = Field(
        DEFAULT_COOLDOWN_SECONDS,
        ge=MIN_COOLDOWN_SECONDS,
        le=MAX_COOLDOWN_SECONDS,
        description="how long an account cools down after a usage limit that gave no reset time",
    )
    failover: bool = Field(True, description="whether a run that hits a usage limit goes on with another account")
    max_failovers: int = Field(
        DEFAULT_MAX_FAILOVERS, ge=0, le=MAX_FAILOVERS, description="at most how many times a run moves on"
    )
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


class AccountPolicy(AccountPolicyWrite):
    project: str
    updated_at: datetime | None = Field(description="when the caller set it; null for the defaults")
