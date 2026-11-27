"""The shapes of the projects' routes (``evo_agents.hub.server.projects``): a registration as a harness sends it, a
project as a caller sees it, and ``ProjectAccess``, one caller's access to one project."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from evo_agents.harness import load_schema
from evo_agents.hub.access import INTEGRITIES, ProjectRules, Refused
from evo_agents.hub.server.base.models import PROJECT_NAME
from evo_agents.hub.server.errors import ErrorBody

SINK_KINDS = tuple(load_schema("knowledge")["$defs"]["sink"]["properties"]["kind"]["enum"])
PRINTABLE = r"^[^\x00-\x1f\x7f]+$"
Name = Annotated[str, Field(min_length=1, max_length=100, pattern=PRINTABLE)]
PathText = Annotated[str, Field(min_length=1, max_length=4096, pattern=PRINTABLE)]
REFUSALS = {403: {"model": ErrorBody}, 409: {"model": ErrorBody}, 422: {"model": ErrorBody}}


class Clearance(BaseModel):
    level: Name
    location: Name | None = None


class Sink(BaseModel):
    id: Name
    kind: Literal[SINK_KINDS]  # the kinds of knowledge.schema.json  # pyright: ignore[reportInvalidTypeForm]
    clearance: Clearance


class LabelIn(BaseModel):
    level: Name
    location: Name | None = Field(None, description="the lowest location of the ladder when left out")
    integrity: Literal[INTEGRITIES] = "U"  # pyright: ignore[reportInvalidTypeForm]


class Repo(BaseModel):
    name: Name
    origin: str | None = Field(None, max_length=2048, pattern=PRINTABLE)
    default_branch: str | None = Field(None, max_length=255, pattern=PRINTABLE)
    path: PathText | None = Field(None, description="relative to the workspace when inside it, else ~/... or absolute")


class Harness(BaseModel):
    name: str = Field(pattern=PROJECT_NAME, description="the name in harness.yaml, the cluster's in the registry")
    workspace: PathText = Field(description="~/... when under the home directory of the machine that registered")
    path: PathText = Field(description="the harness root, relative to the workspace when inside it")


class Registration(BaseModel):
    levels: list[Name] = Field(min_length=1, max_length=32, description="lowest first, as in knowledge.yaml")
    locations: list[Name] = Field(min_length=1, max_length=32, description="least restricted first")
    default_label: LabelIn = Field(description="the harness source's label: memories and plans get it by default")
    sinks: list[Sink] = Field(default_factory=list, max_length=64)
    repos: list[Repo] = Field(default_factory=list, max_length=1000)
    harness: Harness


class Project(BaseModel):
    name: str
    harness: Harness | None = Field(description="null for a project registered without its harness paths")
    levels: list[str]
    locations: list[str]
    default_label: dict
    sinks: list[Sink]
    repos: list[Repo]
    role: str | None = Field(description="the caller's role; null for a hub admin without a grant")
    max_level: str | None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = Field(
        None, description="when it was archived: it takes no new run, kg push or build; null while it is live"
    )
    archived_by: str | None = Field(None, description="the login of whoever archived it; null while it is live")


class Registered(Project):
    created: bool
    changed: bool = Field(description="false when the hub already held exactly this")


@dataclass(frozen=True)
class ProjectAccess:
    """One caller in one project: the project's rules and the caller's grant (role and max level are None
    without one)."""

    project_id: int
    rules: ProjectRules
    role: str | None
    max_level: str | None

    @property
    def name(self) -> str:
        return self.rules.name

    def visible(self, label, sink: str) -> bool:
        """The read rule for an object carrying ``label`` (as stored), read through ``sink`` of the project."""
        return self.rules.visible(label, self.max_level, sink)

    def push_label(self, label=None) -> dict:
        """The label to store a pushed object with: ``label``, or the project's default without one. Raises the
        403 or 422 of the write rule; call it before writing anything, in the transaction that writes."""
        try:
            return self.rules.check_push(label, self.role)
        except Refused as exc:
            raise HTTPException(exc.status, str(exc)) from None


def not_found(name: str) -> str:
    return f"no project {name} that you can see on this hub: see `evo-agents hub project list`"


def clearance_fields(clearance: dict) -> dict:
    """The level and location of a sink's clearance, without the ones left out."""
    return {key: clearance[key] for key in ("level", "location") if clearance.get(key) is not None}
