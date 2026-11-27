"""The shapes of a project's named graphs (``evo_agents.hub.server.kg_graphs``) and the names and sources they take."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import Path as PathParam
from pydantic import BaseModel, Field

from evo_agents.hub import kg_named_graphs as store

GraphName = Annotated[str, PathParam(min_length=1, max_length=63)]
SourceId = Annotated[str, Field(min_length=1, max_length=store.MAX_SOURCE_CHARS, pattern=store.SOURCE_PATTERN)]
SourceList = Annotated[list[SourceId], Field(max_length=store.MAX_SOURCES)]


class NamedGraphOut(BaseModel):
    name: str
    description: str
    sources: list[str] = Field(description="the sources it holds; empty for default, which holds every source")
    default: bool = Field(description="the implicit graph of every source of the project")
    missing: list[str] = Field(
        default_factory=list, description="sources it names that the newest build did not read: nothing of them shows"
    )
    created_by: str | None = None
    updated_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class NamedGraphsOut(BaseModel):
    graphs: list[NamedGraphOut] = Field(description="default first, then the named graphs by name")
    sources: list[str] = Field(description="the sources the newest build of the project read")
    can_change: bool


class NamedGraphCreate(BaseModel):
    name: str = Field(min_length=1, max_length=63, description="a-z, 0-9, '.', '_', '-'; not 'default'")
    sources: SourceList = Field(default_factory=list)
    description: str = Field("", max_length=store.MAX_DESCRIPTION_CHARS)


class NamedGraphUpdate(BaseModel):
    rename: str | None = Field(None, min_length=1, max_length=63, description="its new name")
    description: str | None = Field(None, max_length=store.MAX_DESCRIPTION_CHARS)
    sources: SourceList | None = Field(None, description="its sources, all of them, in place of what it holds")
    add: SourceList = Field(default_factory=list, description="sources it gets")
    remove: SourceList = Field(default_factory=list, description="sources it loses")


class NamedGraphDeleted(BaseModel):
    project: str
    deleted: str
