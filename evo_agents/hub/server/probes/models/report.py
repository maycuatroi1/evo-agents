"""The shapes of the weekly report of the Curator's probes (``evo_agents.hub.server.probes.service.report`` builds
it): a week of results (``ProbeWeek``), the changes without a probe (``ProbeUnprobed``), the fix proposals
(``ProbeFix``) and the replacement probes (``ProbeReplacement``) of the report (``ProbeWeekly``), with the limits and the
words of the report."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub import probes

WEEK = timedelta(days=7)
DEFAULT_WEEKS = 2
MAX_UNPROBED = 50  # the changes without a probe a report names, the newest first
MAX_NOTICE_LISTED = 10  # the flagged probes and the changes without a probe the notice's body names
NOT_YET = "not measured yet this week"
NO_RUN = "no probe run measured it this week"
NOT_PLACED = (
    "it changes no file a probe arm can place (AGENTS.md, CLAUDE.md, a skill, the repo's .claude/settings.json or "
    ".claude/hooks/): a probe cannot measure it"
)
NOT_WRITTEN = "no probe was written for it: a review run or an admin adds one (evo-agents hub curator probe add)"
BORN = "it was born of a correction"
NOT_BORN = "it changes a file a probe arm places, though its evidence cites no correction"

Status = Literal[probes.STATUSES]  # pyright: ignore[reportInvalidTypeForm]


class ProbeArmCount(BaseModel):
    runs: int = Field(description="the arm's sessions")
    failed: int = Field(description="those whose verify did not pass")
    recurrence: float | None = Field(description="1 - pass^k of the arm; null without a session")


class ProbeWeekResult(BaseModel):
    """One probe on one runtime and model in a week."""

    change_id: int | None = Field(description="the change it probes; null: the probe of a plugin's rule")
    probe_id: int
    runtime: str
    model: str | None = Field(description="null: the runtime's own choice")
    k: int | None = Field(description="the sessions of each arm; null when the week queued none")
    on: ProbeArmCount = Field(description="the change's rule in place")
    off: ProbeArmCount = Field(description="the change's diff taken off")
    status: Status  # pyright: ignore[reportInvalidTypeForm]
    previous: Status | None = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="the status of the week before; null when it had none"
    )
    reason: str | None = Field(description="why it is stale or unmeasured")
    run_id: int | None = Field(description="the probe run that measured it last")
    arms_at: Literal[probes.ARMS_AT] | None = Field(  # pyright: ignore[reportInvalidTypeForm]
        None,
        description="where its arms ran: tip (arm on at the tip of the default branch, arm off that tip less the "
        "change's diff) or change (the diff no longer came off the tip: arm on at the change's commit, arm off at the "
        "one before it); null when the week has no session of it, and for a plugin's rule",
    )
    plugin: str | None = Field(None, description="the core plugin whose rule it probes, when it has no change")
    ref: str | None = Field(None, description="the key of that plugin's rule")


class ProbeWeekTotals(BaseModel):
    probes: int = Field(description="the results of the week")
    holds: int
    recurring: int
    not_reproduced: int
    stale: int
    unmeasured: int
    on: ProbeArmCount
    off: ProbeArmCount


class ProbeWeek(BaseModel):
    week: date = Field(description="the Monday of the ISO week, in the charter's time zone")
    complete: bool = Field(description="false for the week in progress")
    cost_usd: float = Field(description="what the week's probe runs cost")
    budget_usd: float | None = Field(description="the charter's probes.budget_usd; null: the project runs no probe")
    probes: list[ProbeWeekResult]
    totals: ProbeWeekTotals


class ProbeUnprobed(BaseModel):
    """A merged change with no active probe that changes a file a probe arm places (``probes.probe_paths``), or that was
    born of a correction (its evidence cites ``corrections`` of the night's figures); the reason says which."""

    change_id: int
    proposal_id: int
    title: str
    merged_at: datetime | None
    reason: str = Field(
        description="why it has no probe; for a change that places a file a probe arm places, whether it was born "
        "of a correction"
    )


class ProbeFix(BaseModel):
    """A proposal that fixes the rule of a change whose probe recurs (criterion a5)."""

    proposal_id: int
    change_id: int
    title: str
    state: str


class ProbeReplacement(BaseModel):
    """A probe a review run wrote in place of one not reproduced, or stale, two weeks in a row (criterion a5)."""

    probe_id: int
    replaces: int = Field(description="the probe it replaced, now retired")
    change_id: int | None
    state: str
    created_at: datetime


class ProbeWeekly(BaseModel):
    project: str
    k: int = Field(description="the charter's probes.k")
    budget_usd: float | None = Field(description="the charter's probes.budget_usd per ISO week; null: no probes")
    weeks: list[ProbeWeek] = Field(description="the newest first")
    unprobed: list[ProbeUnprobed] = Field(
        description="merged changes without an active probe that change a file a probe arm places, or that were born "
        "of a correction, the newest first"
    )
    fixes: list[ProbeFix] = Field(description="the fix proposals of recurring probes, with their state")
    replacements: list[ProbeReplacement] = Field(
        default_factory=list,
        description="the probes written in place of probes not reproduced, or stale, two weeks in a row",
    )


ARMS_AT_WORDS = {
    probes.ARMS_AT_TIP: "arms at the tip of the default branch",
    probes.ARMS_AT_CHANGE: "arms at the change's commits, its diff no longer comes off the tip",
}
