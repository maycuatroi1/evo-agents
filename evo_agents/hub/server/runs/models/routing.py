"""The routing of a run's model by its difficulty (plan super-board-parity-2, criterion a6,
``evo_agents.hub.model_router``): what a claim hands the worker, and what the worker reports it did. Leaf shapes of the
models layer of ``evo_agents.hub.server.runs``, which import nothing of the package; ``models`` imports them back."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from evo_agents.hub import model_router, runs

Model = Annotated[str, Field(min_length=1, max_length=runs.MAX_MODEL_CHARS)]


class RoutingLadder(BaseModel):
    easy: Model
    medium: Model = Field(description="also the model of a run the scorer did not rate")
    hard: Model


class RunRouting(BaseModel):
    """The routing a claim carries while the project's policy has it on, for a run that names no model."""

    scorer: Model = Field(description="the model of the short turn that rates the run's difficulty")
    ladder: RoutingLadder = Field(description="the model of each difficulty")


class RoutingRecord(BaseModel):
    """What the worker's rating of a run did (``model_router.record``)."""

    scorer: Model
    difficulty: Literal[model_router.LEVELS] = Field(
        description=f"the difficulty the scorer named; {model_router.FALLBACK} when it named none"
    )
    scored: bool = Field(description="false: the scorer failed or named no difficulty")
    model: Model | None = Field(
        None, description="the ladder's model the run took; null when its agent profile does not allow it"
    )
    cost_usd: float | None = Field(None, ge=0, description="what the scorer's turn cost; null when not reported")
    error: str | None = Field(
        None, max_length=model_router.MAX_ERROR_CHARS, description="why it did not rate or choose"
    )
