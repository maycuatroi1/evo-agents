"""What a run spent, as Run shows it (``spend``): its tokens and cost, and each model's, from the hub's ledger of its
usage reports (``evo_agents.hub.run_spend``, ``evo_agents.hub.spend_store``)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class SpentTokens(BaseModel):
    input: int = Field(0, description="input tokens not read from the cache")
    output: int = Field(0, description="output tokens, reasoning among them")
    cache_read: int = Field(0, description="input tokens read from the cache")
    cache_write: int = Field(0, description="input tokens written to the cache")
    cache_write_1h: int = Field(0, description="of cache_write, those written for an hour")
    reasoning: int = Field(0, description="of output, the reasoning the runtime counted apart")
    total: int = Field(0, description="input, output, cache read and cache write together")


class ModelSpend(BaseModel):
    model: str = Field(description="the model as its runtime named it; empty when it named none")
    tokens: SpentTokens
    cost_usd: float | None = Field(description="reported plus estimated; null when neither is known")
    reported_usd: float | None = Field(description="the cost its runtime reported; null when it reported none")
    estimated_usd: float | None = Field(
        description="the tokens no reported cost covers, at the model's list price; null for none, or no price"
    )


class RunSpend(BaseModel):
    tokens: SpentTokens
    cost_usd: float | None = Field(description="reported plus estimated; null when neither is known")
    reported_usd: float | None = Field(description="what its runtimes reported it cost; null when none did")
    estimated_usd: float | None = Field(description="what the tokens no reported cost covers come to at list prices")
    models: list[ModelSpend] = Field(description="each model it used, the dearest first")
    sessions: int = Field(description="the agent sessions it spent in")
    updated_at: datetime = Field(description="when the hub last took in its usage")


def _tokens(found: dict) -> SpentTokens:
    figures = {key: int(found.get(key, 0)) for key in ("input", "output", "cache_read", "cache_write")}
    parts = {key: int(found.get(key, 0)) for key in ("cache_write_1h", "reasoning")}
    return SpentTokens(**figures, **parts, total=sum(figures.values()))


def _cost(reported: float | None, estimated: float | None) -> float | None:
    if reported is None and estimated is None:
        return None
    return round((reported or 0.0) + (estimated or 0.0), 6)


def run_spend_view(summary: dict) -> RunSpend:
    """Run's ``spend`` from ``spend_store.spend_of``'s summary of a run."""
    models = [
        ModelSpend(
            model=entry["model"],
            tokens=_tokens(entry["tokens"]),
            cost_usd=_cost(entry["reported_usd"], entry["estimated_usd"]),
            reported_usd=entry["reported_usd"],
            estimated_usd=entry["estimated_usd"],
        )
        for entry in summary["models"]
    ]
    return RunSpend(
        tokens=_tokens(summary["tokens"]),
        cost_usd=_cost(summary["reported_usd"], summary["estimated_usd"]),
        reported_usd=summary["reported_usd"],
        estimated_usd=summary["estimated_usd"],
        models=models,
        sessions=summary["sessions"],
        updated_at=summary["updated_at"],
    )
