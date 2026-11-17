"""The routing of a run's model by its difficulty (plans super-board-parity-2, criterion a6, and
token-and-quota-economy, ``evo_agents.hub.model_router``): what a claim hands the worker, and what the hub and the
worker did. Leaf shapes of the models layer of ``evo_agents.hub.server.runs``, which import nothing of the package;
``models`` imports them back. A key the hub's rating adds is left out of the record while it is null, so a record of a
run the worker alone rated reads as it did before the hub rated runs."""

from __future__ import annotations

from typing import Annotated, Literal, get_args

from pydantic import BaseModel, Field, SerializerFunctionWrapHandler, model_serializer

from evo_agents.hub import model_router, runs

Model = Annotated[str, Field(min_length=1, max_length=runs.MAX_MODEL_CHARS)]
Level = Literal["easy", "medium", "hard"]
Source = Literal["plan", "rule", "role", "scorer", "fallback"]
Strategy = Literal["surplus-first", "cost-first", "quota-burn"]
Branch = Literal["ladder", "overflow"]
for _named, _shared in ((Level, model_router.LEVELS), (Source, model_router.SOURCES),
                        (Strategy, model_router.STRATEGIES), (Branch, model_router.BRANCHES)):  # fmt: skip
    if get_args(_named) != _shared:
        raise RuntimeError(f"the routing models read {get_args(_named)}, the rules {_shared}")


class RoutingLadder(BaseModel):
    easy: Model
    medium: Model = Field(description="also the model of a run the scorer did not rate")
    hard: Model


HUB_KEYS = model_router.HUB_KEYS


def _without_null_hub_keys(found: dict) -> dict:
    return {key: value for key, value in found.items() if key not in HUB_KEYS or value is not None}


class RunRouting(BaseModel):
    """The routing a claim carries while the project's policy has it on, for a run that names no model. When the hub
    rated the run, every rung of the ladder is the model it chose, and ``difficulty`` says what it rated; while the
    rating waits for the scorer (``pending``), the ladder is the strategy's model of each difficulty."""

    scorer: Model = Field(description="the model of the short turn that rates the run's difficulty")
    ladder: RoutingLadder = Field(description="the model of each difficulty")
    difficulty: Level | None = Field(
        None, description="the difficulty the hub rated the run; null: the worker's scorer rates it"
    )
    source: Source | None = Field(None, description="where the difficulty came from")
    reason: str | None = Field(None, max_length=model_router.MAX_REASON_CHARS, description="why that model")
    strategy: Strategy | None = Field(None, description="the Token strategy that chose it")
    branch: Branch | None = Field(None, description="ladder or overflow")
    escalated_from: Level | None = Field(
        None, description="the difficulty of the run before it, which failed, when this one went one level up"
    )
    pending: bool | None = Field(None, description="true: the worker's scorer rates the run among the ladder's models")

    @model_serializer(mode="wrap")
    def _dump(self, handler: SerializerFunctionWrapHandler):
        found = _without_null_hub_keys(handler(self))
        return {key: value for key, value in found.items() if key != "difficulty" or value is not None}


class RoutingRecord(BaseModel):
    """What the worker's rating of a run did (``model_router.record``)."""

    scorer: Model
    difficulty: Level = Field(
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
    source: Source | None = Field(
        None, description="where the difficulty came from: plan, rule, role, scorer or fallback; absent: the scorer"
    )
    reason: str | None = Field(
        None, max_length=model_router.MAX_REASON_CHARS, description="why the hub gave the run its model"
    )
    strategy: Strategy | None = Field(None, description="the Token strategy that chose it")
    branch: Branch | None = Field(None, description="the ladder's model, or the overflow's")
    escalated_from: Level | None = Field(
        None, description="the difficulty of the failed run before this one, when this one went one level up"
    )
    gateway: str | None = Field(
        None, max_length=runs.MAX_MODEL_CHARS, description="the gateway prefix of the chosen model; absent: native"
    )
    quota: dict[str, float] | None = Field(
        None, description="what the subscription had left when the hub chose: weekly_left and five_hour_used, 0 to 1"
    )
    pending: bool | None = Field(None, description="true: the hub left the rating to the worker's scorer")
    choices: dict[Level, Model] | None = Field(
        None, description="while pending: the model of each difficulty the scorer may name"
    )

    @model_serializer(mode="wrap")
    def _dump(self, handler: SerializerFunctionWrapHandler):
        return _without_null_hub_keys(handler(self))
