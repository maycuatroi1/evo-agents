"""The shapes of which features of the hub are on (``evo_agents.hub.server.features``)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub import features
from evo_agents.hub.server.errors import ErrorBody

READ_REFUSALS = {404: {"model": ErrorBody}}
WRITE_REFUSALS = {code: {"model": ErrorBody} for code in (404, 409, 422)}


class FeatureState(BaseModel):
    id: str
    layers: list[Literal[features.LAYERS]] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="where it may be set: instance, the hub's own layer; project, a project's"
    )
    enabled: bool = Field(description="whether it is on for the hub: as the hub's own layer sets it, else on")


class ProjectFeatures(BaseModel):
    project: str
    values: dict[str, bool] = Field(description="the features its layer sets; any other is as the hub has it")


class Features(BaseModel):
    features: list[FeatureState]
    projects: list[ProjectFeatures] = Field(
        description="the projects whose layer sets something, among those the caller may see"
    )
