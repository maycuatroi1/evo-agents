"""The settings of the night's own runs in a charter (``evo_agents.hub.server.curator``, which writes and shows the
charter): how the review run looks (``review``), how the Curator's changes are replayed (``replay``), and how the
history run reads the project's sessions and runs with a cheap model (``history``). Each cost cap is checked against
the night's budget with the charter (``CharterBody``)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from evo_agents.hub import curator, history, replay, review
from evo_agents.hub.server.runs.models import ModelName


class ReviewSettings(BaseModel):
    """How the night's review run of the Curator looks (``evo_agents.hub.review``)."""

    model_config = ConfigDict(extra="forbid")

    lenses: int = Field(
        review.LENSES_PER_NIGHT, ge=1, le=len(review.LENSES), description="lenses a night looks through, in turn"
    )
    days: int = Field(
        review.REVIEW_DAYS, ge=1, le=curator.MAX_REVIEW_DAYS, description="the days of sessions and runs it counts"
    )
    budget_usd: float | None = Field(
        None, gt=0, le=curator.MAX_NIGHT_BUDGET_USD, description="the most the review run may cost; null: as any run"
    )


class ReplaySettings(BaseModel):
    """How the night shift replays the Curator's changes of a skill, an AGENTS.md or a CLAUDE.md on tasks of the
    project's own history (``evo_agents.hub.replay``)."""

    model_config = ConfigDict(extra="forbid")

    tasks: int = Field(
        replay.DEFAULT_TASKS,
        ge=0,
        le=replay.MAX_TASKS,
        description="tasks of the holdout split each change is replayed on",
    )
    budget_usd: float | None = Field(
        None, gt=0, le=curator.MAX_NIGHT_BUDGET_USD, description="the most a night's replay runs cost; null: no replay"
    )


class HistorySettings(BaseModel):
    """How the night's history run reads the project's sessions and runs with a cheap model
    (``evo_agents.hub.history``)."""

    model_config = ConfigDict(extra="forbid")

    budget_usd: float | None = Field(
        None,
        ge=0,
        le=curator.MAX_NIGHT_BUDGET_USD,
        description=f"the most the night's history run may cost; null: the smaller of "
        f"{history.DEFAULT_BUDGET_USD:g} USD and {history.DEFAULT_SHARE:g} of night_budget_usd; 0: no history run",
    )
    model: ModelName | None = Field(
        None,
        description="the model of the reviewer's runtime it runs on; null: the cheapest the worker on duty reports for "
        "that runtime (haiku on Claude Code)",
    )
