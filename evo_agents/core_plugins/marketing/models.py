"""The API of the plugin marketing as Pydantic models, each named ``Marketing...`` in the hub's OpenAPI document. No
model carries a credential: those are sealed in the plugin's config and never come back."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub.tables_marketing import ADAPTERS, CHANNELS, DENY_KINDS, MAX_TEXT, MODES, POST_KINDS

ChannelName = Literal[*CHANNELS]


class MarketingChannelIn(BaseModel):
    channel: ChannelName
    account: str = Field(min_length=1, max_length=200, description="the handle, page or repo the account posts as")
    kind: Literal["project", "personal"] = Field(
        default="project", description="a personal account belongs to the member who makes it and is draft_only"
    )
    adapter: Literal[*ADAPTERS] | None = Field(default=None, description="api or browser; the channel's default")
    target: str | None = Field(
        default=None, max_length=300, description="a subreddit, a page id, owner/name, an author urn"
    )


class MarketingChannel(BaseModel):
    id: int
    channel: str
    account: str
    kind: str
    mode: str = Field(description="autonomous or draft_only")
    adapter: str
    owner: str | None = Field(description="the login of the member who owns a personal account")
    target: str | None
    paused: bool = Field(description="the breaker is open: the channel posts nothing until an admin resumes it")
    paused_reason: str | None
    autonomous_since: datetime | None


class MarketingModeIn(BaseModel):
    mode: Literal[*MODES]


class MarketingReasonIn(BaseModel):
    reason: str = Field(default="stopped by hand", min_length=1, max_length=500)


class MarketingSource(BaseModel):
    quote: str = Field(min_length=1, max_length=500, description="the sentence of the post the source backs")
    url: str = Field(min_length=1, max_length=2000, description="a link to the docs, a release or the changelog")


class MarketingPostIn(BaseModel):
    channel_id: int
    body: str = Field(min_length=1, max_length=MAX_TEXT)
    kind: Literal[*POST_KINDS] = "post"
    reply_to: str | None = Field(default=None, max_length=300, description="what a reply answers")
    links: list[str] = Field(default_factory=list, max_length=20)
    sources: list[MarketingSource] = Field(default_factory=list, max_length=50)
    run_id: int | None = Field(default=None, description="the run that proposed it, kept in the audit")


class MarketingPost(BaseModel):
    id: int
    channel_id: int
    channel: str
    account: str
    kind: str
    body: str
    links: list[str]
    state: str = Field(description="proposed, judging, posted, draft, failed or withdrawn")
    reason: str | None = Field(description="why it is a draft, failed or withdrawn")
    layers: dict = Field(description="layer -> {ok, detail}, in the order the gate ran them")
    judge: dict | None = Field(description="the Judge's scores and verdict")
    run_id: int | None
    posted_url: str | None
    cost_usd: float
    dry_run: bool = Field(description="it would have gone out, but the channel is in its dry-run time")
    created_at: datetime
    posted_at: datetime | None


class MarketingDenyIn(BaseModel):
    kind: Literal[*DENY_KINDS]
    value: str = Field(min_length=1, max_length=200)


class MarketingDeny(BaseModel):
    id: int
    kind: str
    value: str
    added_at: datetime


class MarketingBudget(BaseModel):
    month: str
    budget_usd: float
    reserved_usd: float
    spent_usd: float


class MarketingStatus(BaseModel):
    project: str
    killed: bool = Field(description="the kill switch of the project is set: no channel posts")
    reason: str | None
    channels: int
    open_breakers: int
    budget: MarketingBudget


class MarketingDone(BaseModel):
    ok: bool = Field(description="false when nothing changed (it was so already)")
