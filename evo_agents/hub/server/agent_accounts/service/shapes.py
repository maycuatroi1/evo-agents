"""The shapes of the accounts of an agent: what a PUT and a PATCH of an account or a provider account take, and what
reads as an account. They hold a worker's name (``worker_models``), an account's figures (``account_views``) and a
provider account's ``ModelsFrom`` (``provider_store``), modules of the hub that reach this service, so they are here
and not in the models."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, SecretStr

from evo_agents.hub.account_providers import Category, Provider
from evo_agents.hub.accounts import ACCOUNT_STATES, MAX_PRIORITY, MAX_WEIGHT
from evo_agents.hub.credentials import MAX_SECRET_BYTES
from evo_agents.hub.provider_accounts import API_FORMATS, MAX_MODELS, PROVIDER_ID
from evo_agents.hub.server.account_views import AccountUsage, HeldBy
from evo_agents.hub.server.agent_accounts.models.accounts import MAX_ENV_VAR_CHARS, MAX_WORKERS
from evo_agents.hub.server.agent_accounts.models.provider_accounts import Models
from evo_agents.hub.server.base.models import StrictModel
from evo_agents.hub.server.provider_store import ModelsFrom
from evo_agents.hub.server.workers.models.workers import WORKER_NAME

Workers = list[Annotated[str, Field(pattern=WORKER_NAME)]]


class AgentAccountWrite(BaseModel):
    env_var: str = Field(
        max_length=MAX_ENV_VAR_CHARS,
        description="the variable of the agent's environment it sets, upper case, never one that steers the shell, git "
        "or the worker (PATH, HOME, EVO_*, GIT_* ...)",
    )
    value: SecretStr = Field(
        min_length=1,
        max_length=MAX_SECRET_BYTES,
        description=f"the secret itself, 1 to {MAX_SECRET_BYTES} bytes of UTF-8; written once, never shown again",
    )
    workers: list[Annotated[str, Field(pattern=WORKER_NAME)]] = Field(
        min_length=1,
        max_length=MAX_WORKERS,
        description="names of the workers it is bound to: yours that are not revoked, or a member's shared worker; each "
        "serves the agent's project",
    )
    expires_at: AwareDatetime | None = Field(None, description="after this no run gets it; in the future")
    weight: int | None = Field(None, ge=0, le=MAX_WEIGHT, description="its share in a weighted pool; 1 when new")
    priority: int | None = Field(None, ge=0, le=MAX_PRIORITY, description="the lower, the sooner; 0 when new")


class AgentAccountChange(StrictModel):
    value: SecretStr | None = Field(
        None, min_length=1, max_length=MAX_SECRET_BYTES, description="renew: a new value for the same account"
    )
    workers: list[Annotated[str, Field(pattern=WORKER_NAME)]] | None = Field(
        None, min_length=1, max_length=MAX_WORKERS, description="replaces the workers it is bound to"
    )
    expires_at: AwareDatetime | None = Field(None, description="its new end; null drops the end")
    weight: int | None = Field(None, ge=0, le=MAX_WEIGHT)
    priority: int | None = Field(None, ge=0, le=MAX_PRIORITY)
    paused: bool | None = Field(None, description="true pauses it, so no new run takes it; false resumes it")


class AgentAccount(BaseModel):
    """An account of an agent as an admin sees it: everything but the value."""

    # every member of its project reads it too, with the figures a member's own account has (``account_views``)

    id: int = Field(description="the id of its secret, which audit lines name it by")
    name: str
    env_var: str = Field(description="the variable it sets")
    provider: Provider = Field(description="who it signs in to, read from its variable (``account_providers``)")
    category: Category = Field(description="model: the runtime's own sign-in, whose runs cost; service: any other")
    workers: list[str] = Field(description="the workers it is bound to")
    weight: int
    priority: int
    paused_at: datetime | None
    cooldown_until: datetime | None
    cooldown_reason: str | None
    last_leased_at: datetime | None
    expires_at: datetime | None
    state: Literal[ACCOUNT_STATES]  # pyright: ignore[reportInvalidTypeForm]
    created_at: datetime
    updated_at: datetime = Field(description="when its value was last written")
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


class AgentAccountWritten(AgentAccount):
    created: bool = Field(description="false when an account of this name was replaced")


class ListedAgentAccount(AgentAccount):
    """An account of an agent, with the project and the agent it belongs to."""

    project: str
    handle: str = Field(description="the agent's handle, @ left out")
    agent_name: str = Field(description="the agent's display name")
    color: str = Field(description="the agent's colour, on which the web draws its initial")
    icon_url: str | None = Field(description="GET of the agent's avatar on this hub; null: draw the initial")


class AgentProviderAccountWrite(StrictModel):
    provider: str = Field(pattern=PROVIDER_ID.pattern, description="the id of an entry of the catalog")
    base_url: str | None = Field(None, description="the https root of its API; the catalog's when left out")
    api_format: Literal[API_FORMATS] | None = Field(None, description="the catalog's when left out")  # pyright: ignore[reportInvalidTypeForm]
    value: SecretStr = Field(min_length=1, max_length=MAX_SECRET_BYTES, description="the API key; never shown again")
    models: Models = Field(default_factory=list, max_length=MAX_MODELS, description="model names, as typed")
    models_from: ModelsFrom | None = Field(None, description="add the catalog's default model, or the endpoint's")
    workers: Workers = Field(min_length=1, max_length=MAX_WORKERS, description="workers serving the agent's project")
    expires_at: AwareDatetime | None = Field(None, description="after this no run takes it; in the future")
    weight: int | None = Field(None, ge=0, le=MAX_WEIGHT)
    priority: int | None = Field(None, ge=0, le=MAX_PRIORITY)
    paused: bool | None = None


class AgentProviderAccountChange(StrictModel):
    base_url: str | None = None
    api_format: Literal[API_FORMATS] | None = None  # pyright: ignore[reportInvalidTypeForm]
    value: SecretStr | None = Field(None, min_length=1, max_length=MAX_SECRET_BYTES, description="a new API key")
    models: Models | None = Field(None, max_length=MAX_MODELS)
    models_from: ModelsFrom | None = None
    workers: Workers | None = Field(None, min_length=1, max_length=MAX_WORKERS)
    expires_at: AwareDatetime | None = Field(None, description="null drops the end")
    weight: int | None = Field(None, ge=0, le=MAX_WEIGHT)
    priority: int | None = Field(None, ge=0, le=MAX_PRIORITY)
    paused: bool | None = Field(None, description="true pauses it, false resumes it")
