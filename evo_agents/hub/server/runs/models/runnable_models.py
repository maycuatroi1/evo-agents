"""The response model of GET /v1/projects/{project}/runnable-models (``evo_agents.hub.server.runnable_models``)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RunnableModel(BaseModel):
    model: str = Field(description="as a run asks for it: <prefix>/<model>")
    prefix: str = Field(description="the catalog's alias of its provider, or evo-<id> for an endpoint of a member")
    runtimes: list[str] = Field(description="the runtimes that run it through the model gateway; any picks one")
    accounts: list[str] = Field(description="the names of the provider accounts that serve it, by name")
