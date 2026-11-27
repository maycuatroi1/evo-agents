"""The build the hub's web pages of a project's graph read (``evo_agents.hub.server.kg_web``)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

GRAPHS_TRIED = 10  # kept graphs a page may find its snapshot among: the newest, or one it still holds


class GraphRef(BaseModel):
    build_id: int
    content_hash: str
    nodes: int
    edges: int
    finished_at: datetime
    latest: bool = Field(description="false when the blob store did not answer and an older cached build answered")
