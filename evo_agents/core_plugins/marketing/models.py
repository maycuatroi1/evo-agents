"""The API of the plugin marketing as Pydantic models, each named ``Marketing...`` in the hub's OpenAPI document. No
model carries a credential: those are sealed in the plugin's config and never come back."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub.marketing_browser import OUTCOMES
from evo_agents.hub.tables_marketing import ADAPTERS, CHANNELS, DENY_KINDS, MAX_TEXT, MODES, POST_KINDS, SESSION_STATES

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
    session_state: str = Field(
        default="none", description="none, ok or stale: whether the login session of a browser channel works"
    )


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
    claim: str | None = Field(
        default=None, pattern=r"^CL-[0-9]{1,9}$", description="the canvas claim the post tests, as CL-<n>"
    )
    signal_key: str | None = Field(
        default=None, min_length=1, max_length=200, description="the Signal key the post's numbers count toward"
    )
    scheduled_at: datetime | None = Field(
        default=None, description="when the gate decides it; a time to come keeps it waiting until the Loop releases it"
    )


class MarketingPost(BaseModel):
    id: int
    channel_id: int
    channel: str
    account: str
    kind: str
    body: str
    links: list[str]
    state: str = Field(description="proposed, judging, queued, posting, posted, draft, failed or withdrawn")
    reason: str | None = Field(description="why it is a draft, failed or withdrawn")
    layers: dict = Field(description="layer -> {ok, detail}, in the order the gate ran them")
    judge: dict | None = Field(description="the Judge's scores and verdict")
    run_id: int | None
    posted_url: str | None
    cost_usd: float
    dry_run: bool = Field(description="it would have gone out, but the channel is in its dry-run time")
    created_at: datetime
    posted_at: datetime | None
    claim_id: int | None = Field(default=None, description="the canvas claim the post tests")
    signal_key: str | None = Field(default=None, description="the Signal key its numbers count toward")
    scheduled_at: datetime | None = Field(default=None, description="when the gate decides it")


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


class MarketingSessionIn(BaseModel):
    state: Literal[*SESSION_STATES] = Field(
        description="ok once the account's owner logged in and the session is sealed"
    )


class MarketingBrowserJob(BaseModel):
    """What a worker's script posts. ``session`` is the sealed login session of the account: it goes to the script and
    to no other answer of the plugin."""

    post_id: int
    channel: str
    account: str
    target: str | None
    body: str
    session: str = Field(repr=False, description="Playwright's storage state as JSON")


class MarketingBrowserClaim(BaseModel):
    job: MarketingBrowserJob | None = Field(description="the next post to make, none when nothing waits")


class MarketingBrowserResultIn(BaseModel):
    outcome: Literal[*OUTCOMES]
    url: str | None = Field(default=None, max_length=2000, description="the link of the post, when it went out")
    detail: str = Field(default="", max_length=1000, description="what the script saw, never the page's text")
    screenshot: str | None = Field(default=None, description="a PNG after the post or where the script stopped, base64")


class MarketingBrowserResult(BaseModel):
    state: str = Field(description="posted or failed")
    reason: str | None


class MarketingGoalSignal(BaseModel):
    code: str = Field(description="G-<n>")
    statement: str
    metric: str = Field(description="how the goal's metric reads")
    value: float | None = Field(description="the metric's value now; none when no signal is in its window")
    target: float | None
    reached: bool | None = Field(description="whether the value meets the target; none without a value")


class MarketingWeekPost(BaseModel):
    id: int
    channel: str
    account: str
    posted_url: str | None
    posted_at: datetime | None
    cost_usd: float


class MarketingWeekBreaker(BaseModel):
    channel_id: int
    channel: str
    account: str
    reason: str | None
    paused_at: datetime | None


class MarketingWeekProposal(BaseModel):
    id: int = Field(description="the post that holds the content for a member's personal channel")
    channel: str
    account: str
    state: str
    created_at: datetime


class MarketingWeekly(BaseModel):
    """The Growth section of the project's weekly bulletin: the 7 days up to ``until``."""

    project: str
    since: datetime
    until: datetime
    goals: list[MarketingGoalSignal] = Field(description="the active goals with a metric, against their target")
    posted: list[MarketingWeekPost] = Field(description="the posts that went out in the week, dry runs excluded")
    counts: dict[str, int] = Field(description="the posts proposed in the week, by where the gate left them")
    breakers: list[MarketingWeekBreaker] = Field(description="the channels whose breaker is open now")
    killed: bool
    reason: str | None
    budget: MarketingBudget
    week_cost_usd: float = Field(description="what the posts of the week cost")
    proposals: list[MarketingWeekProposal] = Field(
        description="content for members' personal channels in the week: the proposals for the blog project"
    )
    proposals_project: str | None = Field(description="the hub project the proposals go to; none when not set")
