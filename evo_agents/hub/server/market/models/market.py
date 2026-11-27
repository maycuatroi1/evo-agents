"""The shapes of the markets and their plugins (``evo_agents.hub.server.market.service.market``), and the path
parameters every route under /v1/market takes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Path
from pydantic import BaseModel, Field

from evo_agents.hub.server.errors import ErrorBody
from evo_agents.plugins.manifest import PLUGIN_ID
from evo_agents.plugins.market import REPO

STATUSES = ("not_installed", "installed", "upgrade", "in_image", "not_in_image", "needs_license", "unavailable")
Status = Literal["not_installed", "installed", "upgrade", "in_image", "not_in_image", "needs_license", "unavailable"]
MAX_QUERY_CHARS = 200
NO_KEY = (
    "this hub keeps no secrets: a private market's token needs EVO_HUB_SECRETS_KEY, the key that seals it; ask its "
    "operator to set it"
)
PluginId = Annotated[str, Path(pattern=PLUGIN_ID, description="the plugin's id, as the market lists it")]
SourceId = Annotated[int, Path(ge=1, description="the market source's id, as GET /v1/market/sources gives it")]
REFUSALS: dict[int | str, dict[str, Any]] = {404: {"model": ErrorBody}}
WRITE_REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (404, 409, 422, 503)}
ANSWERS: dict[int | str, dict[str, Any]] = {401: {"model": ErrorBody}, 403: {"model": ErrorBody}}


class MarketPermission(BaseModel):
    name: str
    text: str = Field(description="what the permission lets the plugin do, as a hub admin grants it")


class MarketRelease(BaseModel):
    tag: str | None = None
    version: str | None = None
    permissions: list[MarketPermission] = Field(default_factory=list)
    error: str | None = Field(None, description="why the latest release does not read, when it does not")


class MarketPublisher(BaseModel):
    id: str
    name: str | None = None
    signer: str = Field(description="what the market says signs its releases: sigstore:<workflow> or ed25519:<key>")
    identity: str = Field(description="how the signer reads among a publisher's identities")


class MarketPrice(BaseModel):
    kind: Literal["free", "subscription"]
    usd_per_hub_month: float | None = None
    seller: str | None = None


class MarketPlugin(BaseModel):
    source: int = Field(description="the id of the market source that lists it")
    market: str = Field(description="the market's name")
    id: str
    name: str
    description: str
    industries: list[str]
    categories: list[str]
    repo: str
    publisher: MarketPublisher
    tier: Literal["isolated", "core"]
    package: str | None = None
    price: MarketPrice
    icon: str | None = None
    homepage: str | None = None
    license: str | None = None
    requires: str | None = None
    status: Status
    installed_version: str | None = None
    latest: MarketRelease | None = Field(None, description="its latest release, for a free isolated plugin")


class MarketPluginDetail(MarketPlugin):
    trusted: bool = Field(description="whether the hub trusts the signer for the publisher already")
    may_trust: bool = Field(description="whether a hub admin may trust it here (not locked, not a core publisher)")


class MarketBrief(BaseModel):
    id: int
    repo: str
    name: str | None = None
    title: str | None = None
    fetched_at: datetime | None = None
    error: str | None = None


class MarketList(BaseModel):
    plugins: list[MarketPlugin]
    industries: list[str] = Field(description="every industry the markets list, to filter by")
    categories: list[str] = Field(description="every category the markets list, to filter by")
    markets: list[MarketBrief]


class MarketSource(MarketBrief):
    ref: str
    path: str
    builtin: bool = Field(description="the operator's market (EVO_HUB_MARKET_DEFAULT_SOURCE), which stays")
    private: bool = Field(description="whether the hub reads it with a token")
    added_by: str | None = None
    added_at: datetime
    plugins: int = Field(description="how many plugins its index lists")


class MarketSources(BaseModel):
    sources: list[MarketSource]
    default_source: str | None = Field(description="EVO_HUB_MARKET_DEFAULT_SOURCE")
    publishers_locked: bool = Field(description="EVO_HUB_PLUGIN_PUBLISHERS_LOCKED: hub admins may trust no publisher")


class MarketSourceIn(BaseModel):
    repo: Annotated[str, Field(pattern=f"^{REPO.pattern}$", description="OWNER/REPO")]
    ref: Annotated[str, Field(min_length=1, max_length=255)] = "main"
    path: Annotated[str, Field(min_length=1, max_length=500, pattern=r"^[^\s]+$")] = "market.json"
    token: Annotated[str | None, Field(min_length=1, max_length=500)] = Field(
        None, description="a GitHub token that reads a private repo; sealed, never shown again"
    )
