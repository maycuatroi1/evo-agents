"""The shapes of the agent systems of a project: the bodies of the routes of ``routes.systems``, the refusal of a write
with the system as the hub holds it, and the path's slug."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Path
from pydantic import BaseModel, Field

from evo_agents.hub import system_spec
from evo_agents.hub.server.errors import ErrorBody

REVISION_CONFLICT = "revision_conflict"
Slug = Annotated[str, Path(min_length=1, max_length=63, pattern=system_spec.SLUG_PATTERN)]


class ProblemOut(BaseModel):
    level: Literal["error", "warning"] = Field(description="an error stops a write; a warning does not")
    path: str = Field(description="where in the spec, as nodes[2].version or edges[0]; empty for the spec whole")
    message: str
    node: str | None = Field(None, description="the id of the node it is about, to show it there")
    edge: int | None = Field(None, description="the index of the edge it is about in edges")


class Pin(BaseModel):
    node: str
    kind: Literal["agent", "orchestrator"]
    profile: str = Field(description="the handle of the profile")
    version: int | None = Field(description="the version of the profile the node pins")
    latest_version: int | None = Field(description="the profile's version now; null when the project has none")
    outdated: bool = Field(description="the profile has a newer version than the one pinned")
    deleted: bool = Field(description="the profile was deleted: it takes no new run")


class SystemSummary(BaseModel):
    slug: str
    name: str
    revision: int
    entry_agent: str | None = Field(description="the handle of the agent its entry interacts with; null for none")
    nodes: int
    edges: int
    updated_by: str | None
    updated_at: datetime
    default: bool = Field(False, description="the project's default system (`hub system default`)")


class SystemOut(BaseModel):
    project: str
    slug: str
    name: str
    revision: int = Field(description="the revision shown")
    latest_revision: int
    spec: dict = Field(description="the spec at that revision, evo-agent-system/1")
    pins: list[Pin] = Field(description="the agent and orchestrator nodes, with the versions they pin")
    problems: list[ProblemOut] = Field(description="what is wrong in the spec against the project now")
    entry_agent: str | None
    can_change: bool = Field(description="the caller holds the admin role on the project")
    created_by: str | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime


class SystemWritten(SystemOut):
    created: bool
    changed: bool = Field(description="false when the hub already held exactly this; no revision was added")


class SystemRevision(BaseModel):
    revision: int
    actor: str | None
    created_at: datetime


class SystemPut(BaseModel):
    spec: dict = Field(description="evo-agent-system/1: schema, name, purpose, limits, nodes, edges")
    if_revision: int | None = Field(
        None, ge=0, description="the revision it is at now; none or 0 creates it, and is 409 when it exists"
    )


class SystemCheck(BaseModel):
    spec: dict
    slug: str | None = Field(None, description="the system it would replace, whose entry agent it may keep")


class SystemChecked(BaseModel):
    valid: bool = Field(description="no error: PUT would store it")
    spec: dict = Field(description="the spec as PUT would store it, defaults filled in and versions pinned")
    problems: list[ProblemOut]


class SystemDeleted(BaseModel):
    project: str
    deleted: str


class SystemConflict(ErrorBody):
    current: SystemOut | None = Field(None, description="the system as the hub holds it")


WRITE_REFUSALS: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    409: {"model": SystemConflict},
    422: {"model": ErrorBody},
}


class Refused(Exception):
    """A write refused with the body ``SystemConflict``: its status, message, error code, detail and the system."""

    def __init__(self, status: int, message: str, *, code=None, detail=None, current=None):
        super().__init__(message)
        self.status, self.message, self.code, self.detail, self.current = status, message, code, detail, current
