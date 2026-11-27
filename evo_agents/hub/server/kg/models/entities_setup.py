"""The shape of the entities step's setup (``evo_agents.hub.server.kg_entities_setup``) and the strategy a click
turns on."""

from __future__ import annotations

from pydantic import BaseModel, Field

from evo_agents.hub.server.kg.models.graph import Build

ENTITY_STRATEGY = "entity-graph-api"  # the built-in prose strategy whose model the build calls itself, on the hub too
DEFAULT_REASON = "entity graph view"
IN_FLIGHT = ("queued", "running")
NO_CONFIG = "project {project} has no knowledge config on the hub yet: `evo-agents hub kg push` sends it first"


class SetupOut(BaseModel):
    strategy: str | None = Field(description="the strategy the project's builds follow; null for none")
    step_on: bool = Field(description="whether that strategy has the entities step")
    turns_on: str = Field(description="the strategy a writer's click sets while the step is off")
    sink: str | None = Field(description="the sink the entities mapping sends text through; null for a local model")
    sink_problem: str | None = Field(description="why that sink would take no item, as a build says; null when it can")
    can_change: bool = Field(description="whether the reader may turn the step on and queue a build (the writer role)")
    building: Build | None = Field(description="the newest build queued or running")
    queued: bool | None = Field(None, description="POST only: false when a build was waiting already")
