"""The shapes of a provider account as its owner sees it and of a probe of its endpoint, and the path parameters and
limits of their routes; the writes, which hold a project's and a worker's name, are in
``evo_agents.hub.server.accounts.service.shapes``."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Path
from pydantic import BaseModel, Field, SecretStr

from evo_agents.hub.accounts import ACCOUNT_STATES, MAX_PRIORITY, MAX_WEIGHT
from evo_agents.hub.credentials import MAX_SECRET_BYTES, SECRET_NAME
from evo_agents.hub.provider_accounts import API_FORMATS, PROVIDER_ID
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.share_views import CredentialShare

ModelsFrom = Literal["endpoint", "catalog"]
NO_KEY_GIVEN = "give the API key: value, or from_secret, the name of one of your env secrets that holds it"
MAX_BINDINGS = 100
NO_ACCOUNT = "you have no provider account named {name} on this hub: see GET /v1/provider-accounts"
AccountName = Annotated[
    str, Path(pattern=f"^{SECRET_NAME.pattern}$", description="unique among the caller's secrets and accounts")
]
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422, 503)}
Models = list[Annotated[str, Field(min_length=1, max_length=200)]]


class ProviderAccount(BaseModel):
    """A provider account as its owner sees it: everything but its key."""

    name: str
    provider: str = Field(description="the id of its entry in the catalog (GET /v1/model-catalog)")
    provider_name: str = Field(description="the catalog's name of that provider")
    alias: str = Field(description="what the gateway reads before the slash of a model name: <alias>/<model>")
    base_url: str = Field(description="the https root of its API; every address it resolves to is public")
    api_format: Literal[API_FORMATS]  # pyright: ignore[reportInvalidTypeForm]
    models: list[str] = Field(description="the models it serves, by the endpoint's names")
    projects: list[str] = Field(description="the projects whose runs may take it")
    workers: list[str] = Field(description="the workers it is bound to; empty for any worker of its owner")
    weight: int = Field(description=f"0 to {MAX_WEIGHT}: its share in a weighted-round-robin pool; 0 leaves it")
    priority: int = Field(description=f"0 to {MAX_PRIORITY}: the lower, the sooner a fill-first pool takes it")
    paused_at: datetime | None = Field(description="when its owner paused it; null while it may be taken")
    cooldown_until: datetime | None = Field(description="until when it cools down after a usage limit")
    cooldown_reason: str | None = Field(description="why it cools down")
    last_leased_at: datetime | None = Field(description="when a run last took it")
    expires_at: datetime | None
    state: Literal[ACCOUNT_STATES] = Field(description="ready, cooling, paused or expired, now")  # pyright: ignore[reportInvalidTypeForm]
    shares: list[CredentialShare] = Field(default_factory=list, description="its shares not revoked, newest first")
    created_at: datetime
    updated_at: datetime


class ProviderAccountWritten(ProviderAccount):
    created: bool = Field(description="true when this write created it, false when it replaced one")


class ProviderProbe(BaseModel):
    """What the endpoint answered a call with the key: never the key, nor more than a line of its words."""

    ok: bool = Field(description="whether the endpoint took the key")
    status: int | None = Field(description="the HTTP status of its answer; null when none came")
    count: int = Field(description="how many models it listed")
    models: list[str] = Field(description="the models it listed, by its names")
    error: str | None = Field(description="why not, sanitized: no key, at most one short line of what it said")


class ProbeWrite(BaseModel):
    """An endpoint to try before an account is saved: the same fields as the write, the key included."""

    provider: str = Field(pattern=PROVIDER_ID.pattern, description="the id of an entry of the catalog")
    base_url: str | None = Field(None, description="the https root of its API; the catalog's when left out")
    api_format: Literal[API_FORMATS] | None = Field(None, description="the catalog's when left out")  # pyright: ignore[reportInvalidTypeForm]
    value: SecretStr | None = Field(None, min_length=1, max_length=MAX_SECRET_BYTES, description="the API key")
    from_secret: str | None = Field(None, description="or the name of one of your env secrets that holds it")
    model: str | None = Field(None, description="a model to ask, when the endpoint lists none")


class StoredProbe(BaseModel):
    model: str | None = Field(None, description="a model to ask, when the endpoint lists none; its first otherwise")
