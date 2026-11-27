"""The request of an agent's read of another project: its project is checked against ``admin.PROJECT_NAME``, and
``admin`` reaches this package's services, so the shape is here, not in ``evo_agents.hub.server.agents.models``."""

from __future__ import annotations

from pydantic import BaseModel, Field

from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.agents.models.reads import MAX_IDS, MAX_LIMIT, Tool


class CrossReadIn(BaseModel):
    project: str = Field(pattern=PROJECT_NAME, description="the project read, another than the run's")
    tool: Tool = Field(description="what is read, as the evo-hub tool of the same name reads it")
    query: str | None = Field(None, min_length=1, max_length=500, description="kg_search, kg_context, memory_search")
    ids: list[str] = Field(default_factory=list, max_length=MAX_IDS, description="kg_context: node ids")
    memory_id: int | None = Field(None, ge=1, description="memory_get")
    limit: int = Field(10, ge=1, le=MAX_LIMIT, description="kg_search, memory_search")
    graph: str | None = Field(
        None,
        min_length=1,
        max_length=63,
        description="kg_search, kg_context: a named graph of the project read, only its sources' nodes and edges",
    )
