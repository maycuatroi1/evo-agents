"""The shapes and limits of Today, GET /v1/me/today and POST /v1/me/today/seen (``evo_agents.hub.server.today``):
what waits for the caller and what changed since they last looked."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field

from evo_agents.hub import runs
from evo_agents.hub.server.base.models import StrictModel

MAX_ITEMS = 8
MAX_NEEDS_YOU = 20
MAX_PROPOSALS = 200  # the open proposals of the window one answer reads, before their labels are checked
DEFAULT_WINDOW = timedelta(hours=24)  # the window of a member who has no seen mark yet
MAX_WINDOW = timedelta(days=7)  # a seen mark older than this counts from this long ago
METRIC_DAYS = timedelta(days=7)  # a problem's metric now is compared with its value this long ago
ITEM_KINDS = ("release", "step_done", "run_failed", "run_parked", "problem", "cost", "proposal", "report")
FAILED = ("failed", "lost")  # a run of these ends is a run_failed item
TITLE_CHARS = 160
BODY_CHARS = 300
DONE_IDS = '$.steps[*] ? (@.status == "done").id'  # the ids of a plan body's steps that are done, as a JSON path
SEEN, NO_MARK, OLD_MARK = "since you last looked", "in the last 24 hours", "in the last 7 days"  # the window, in words


class TodayDecision(BaseModel):
    id: int
    project: str
    run_id: int = Field(description="the run the decision belongs to")
    plan_id: str | None = Field(description="the run's plan when its agent asked; null for a run on none")
    plan_title: str | None
    step_key: str | None = Field(description="the step it is about, when the agent named one")
    category: Literal[runs.SHOWN_DECISION_CATEGORIES]  # pyright: ignore[reportInvalidTypeForm]
    question: str
    asked_at: datetime
    parks_at: datetime | None = Field(
        description="when the run parks for want of an answer: EVO_HUB_DECISION_WAIT_SECONDS after it started "
        "waiting, or when it parked; null while its agent still works, and for a local run"
    )
    link: str = Field(description="the web page that shows it and its answer form")


class TodayItem(BaseModel):
    kind: Literal[ITEM_KINDS] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="release: a release published; step_done: a plan step done; run_failed and run_parked: a run you "
        "dispatched failed or parked; problem: the metric of a problem you own moved; cost: what your runs cost; "
        "proposal: a proposal of the Curator you may answer; report: notices that only report, such as pushes, merges, "
        "plans finished and files of runs"
    )
    title: str
    body: str | None
    at: datetime = Field(description="when it happened")
    project: str
    link: str = Field(description="the web page it is on")


class Today(BaseModel):
    summary: str = Field(description="one sentence of what waits and what changed, from a template")
    since: datetime = Field(description="the start of the window the items are counted from")
    seen_at: datetime | None = Field(description="your seen mark; null before your first POST /v1/me/today/seen")
    seen_until: datetime = Field(
        description="the seen mark to post once every item listed was shown: the moment of this answer when it is "
        "caught up, else the moment of the last item"
    )
    needs_you: list[TodayDecision] = Field(
        max_length=MAX_NEEDS_YOU, description=f"at most {MAX_NEEDS_YOU} open decisions asked of you, the oldest first"
    )
    needs_you_count: int = Field(description="all the open decisions asked of you")
    items: list[TodayItem] = Field(
        max_length=MAX_ITEMS,
        description=f"at most {MAX_ITEMS} things that changed since the window's start, the first in the order they "
        "happened",
    )
    caught_up: bool = Field(description="whether the items are everything that changed in the window")


class TodaySeenIn(StrictModel):
    seen_at: AwareDatetime | None = Field(
        None, description="what you saw up to, as seen_until gave it; now when left out; a moment past now is now"
    )


class TodaySeen(BaseModel):
    seen_at: datetime = Field(description="your seen mark now, which only moves forward")


@dataclass(frozen=True)
class Found:
    """An item, with what its cost item spent (0 for any other)."""

    item: TodayItem
    cost_usd: float = 0.0
