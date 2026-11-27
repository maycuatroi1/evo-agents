"""The shapes of a project's knowledge config and of a source taken off the hub
(``evo_agents.hub.server.kg_config``)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from evo_agents.hub.server.kg.models.graph import Queued


class KgConfig(BaseModel):
    knowledge: dict = Field(
        description="policy, identifiers, ontology file name and sources {id, connector, label, code, repo, ref}"
    )
    ontology: dict | None = Field(None, description="the ontology extension knowledge.yaml points at, if any")


class ConfigSaved(BaseModel):
    digest: str
    changed: bool = Field(description="false when the hub held exactly this config")


class SourceForgotten(Queued):
    source: str
    ingested: int = Field(description="runs of the source taken out of the project's corpus")
    pending: int = Field(description="runs of the source that waited for their blobs, dropped")
    logs: int = Field(description="their logs nothing else refers to, deleted from the blob store by the next prune")
    declared: bool = Field(
        description="whether the knowledge config on the hub still declares the source: a machine holding its runs "
        "then pushes them again"
    )
    uploads: int = Field(0, description="uploaded archives of the source deleted, with their items")
