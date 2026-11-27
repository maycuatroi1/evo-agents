"""The settings of the night's own runs in a charter (``evo_agents.hub.server.curator``, which writes and shows the
charter): how the review run looks (``review``), how the Curator's changes are replayed (``replay``), and how the
history run reads the project's sessions and runs with a cheap model (``history``), how the probes of the Curator's
changes run (``probes``), and how the Curator evolves a harness or an agent's configuration (``evolve``). Each cost cap of a night is checked against the night's budget with the charter
(``CharterBody``); the probes' budget is a week's."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from evo_agents.hub import curator, evolve, history, playbook, probes, replay, review, runs
from evo_agents.hub.server.base.models import StrictModel
from evo_agents.hub.server.runs.models import ModelName


class ReviewTrigger(StrictModel):
    """When the review run of a night or period is worth queuing (``curator.review_wait``): enough new evidence since
    the previous review and a gap after it, or a long enough gap alone."""

    min_new: int = Field(
        1,
        ge=curator.REVIEW_TRIGGER_NEW[0],
        le=curator.REVIEW_TRIGGER_NEW[1],
        description="new evidence since the previous review that queues one: failed runs of the agent's cause, new "
        "traces of an agent system with an optimizes edge, and turns of a person that correct the agent",
    )
    min_gap_hours: int = Field(
        24,
        ge=curator.REVIEW_TRIGGER_GAP_HOURS[0],
        le=curator.REVIEW_TRIGGER_GAP_HOURS[1],
        description="the hours after the previous review before new evidence queues the next one",
    )
    max_gap_days: int = Field(
        7,
        ge=curator.REVIEW_TRIGGER_DAYS[0],
        le=curator.REVIEW_TRIGGER_DAYS[1],
        description="the days after the previous review that queue the next one whatever the evidence",
    )

    @model_validator(mode="after")
    def _gaps(self):
        if self.min_gap_hours > self.max_gap_days * 24:
            raise ValueError("review.trigger.min_gap_hours is longer than max_gap_days")
        return self


class ReviewSettings(StrictModel):
    """How the night's review run of the Curator looks (``evo_agents.hub.review``)."""

    lenses: int = Field(
        review.LENSES_PER_NIGHT, ge=1, le=len(review.LENSES), description="lenses a night looks through, in turn"
    )
    days: int = Field(
        review.REVIEW_DAYS, ge=1, le=curator.MAX_REVIEW_DAYS, description="the days of sessions and runs it counts"
    )
    budget_usd: float | None = Field(
        None, gt=0, le=curator.MAX_NIGHT_BUDGET_USD, description="the most the review run may cost; null: as any run"
    )
    trigger: ReviewTrigger | None = Field(
        None,
        description="queue the review run of a night or period only when enough new evidence came since the previous "
        "one; null: one every night or period, its figures over days",
    )


class ReplaySettings(StrictModel):
    """How the night shift replays the Curator's changes of a skill, an AGENTS.md or a CLAUDE.md on tasks of the
    project's own history (``evo_agents.hub.replay``)."""

    tasks: int = Field(
        replay.DEFAULT_TASKS,
        ge=0,
        le=replay.MAX_TASKS,
        description="tasks of the holdout split each change is replayed on",
    )
    budget_usd: float | None = Field(
        None, gt=0, le=curator.MAX_NIGHT_BUDGET_USD, description="the most a night's replay runs cost; null: no replay"
    )


class HistorySettings(StrictModel):
    """How the night's history run reads the project's sessions and runs with a cheap model
    (``evo_agents.hub.history``)."""

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


class ProbeModel(StrictModel):
    """A runtime and model the arms of a probe run on."""

    runtime: Literal[runs.RUNTIMES] = curator.DEFAULT_RUNTIME
    model: ModelName | None = Field(None, description="as the runtime names it; null: the runtime's own choice")


class ProbeSettings(StrictModel):
    """How the probes of the Curator's changes run (``evo_agents.hub.probes``): how many arms of each probe, on which
    models, and the most they cost in an ISO week of the charter's time zone."""

    k: int = Field(probes.DEFAULT_K, ge=1, le=probes.MAX_K, description="arms of each probe, on each model")
    models: list[ProbeModel] = Field(
        default_factory=list,
        max_length=probes.MAX_MODELS,
        description="the runtimes and models the arms run on, each once; empty: the charter's builder",
    )
    budget_usd: float | None = Field(
        None,
        gt=0,
        le=probes.MAX_WEEK_BUDGET_USD,
        description="the most the probe runs of an ISO week in the charter's time zone cost; null: no probes run",
    )

    @field_validator("models")
    @classmethod
    def _once(cls, models: list[ProbeModel]) -> list[ProbeModel]:
        seen = [(item.runtime, item.model) for item in models]
        repeated = sorted(
            {f"{runtime} {model or '(default)'}" for runtime, model in seen if seen.count((runtime, model)) > 1}
        )
        if repeated:
            raise ValueError(f"probes.models names each runtime and model once: {', '.join(repeated)} is named twice")
        return models


EVOLVE_ITERATIONS = (6, 50)  # rounds of an evolution's loop: the default, the most
EVOLVE_CANDIDATES = (12, 200)  # candidates it measures: the default, the most
MAX_EVOLVE_PROBES = 20
MAX_EXPOSURE_LIMIT = 1000
PathGlob = Annotated[str, Field(min_length=1, max_length=curator.MAX_PATH_CHARS, pattern=r"^[^\x00-\x1f\x7f]+$")]


class EvolveBudget(StrictModel):
    """What one evolution may spend (``evo_agents.hub.server.evolve``): rounds of its loop, candidates it measures,
    and dollars."""

    iterations: int = Field(EVOLVE_ITERATIONS[0], ge=1, le=EVOLVE_ITERATIONS[1], description="rounds of its loop")
    max_candidates: int = Field(
        EVOLVE_CANDIDATES[0], ge=1, le=EVOLVE_CANDIDATES[1], description="candidates it measures at most"
    )
    max_cost_usd: float | None = Field(
        None, gt=0, le=curator.MAX_NIGHT_BUDGET_USD, description="the most it may cost; null: the run's own cap"
    )


class EvolveSettings(StrictModel):
    """Whether and how the Curator evolves a harness or an agent's configuration (``evo_agents.hub.evolve``): the
    paths it may rewrite, what one evolution spends, how many light tasks of the select split and probes it measures
    on, and how many review runs show an entry of the playbook never found helpful before it is retired."""

    enabled: bool = Field(False, description="false: no evolution is started or queued")
    evolvable_paths: list[PathGlob] = Field(
        default_factory=list,
        max_length=curator.MAX_PROTECTED_PATHS,
        description="globs of harness files an evolution may rewrite, repo:glob for one repo; empty: none",
    )
    budget: EvolveBudget = Field(default_factory=lambda: EvolveBudget.model_validate({}))
    tasks: int = Field(
        evolve.DEFAULT_TASKS, ge=0, le=replay.MAX_TASKS, description="light replay tasks of the select split"
    )
    probes: int = Field(evolve.DEFAULT_PROBES, ge=0, le=MAX_EVOLVE_PROBES, description="active probes of its paths")
    exposure_limit: int | None = Field(
        None,
        ge=0,
        le=MAX_EXPOSURE_LIMIT,
        description=f"review runs that show an entry of the playbook never found helpful before it is retired; null: "
        f"{playbook.EXPOSURE_LIMIT}; 0: never for that",
    )

    @field_validator("evolvable_paths")
    @classmethod
    def _once(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))
