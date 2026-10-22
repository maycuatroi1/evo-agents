"""The shapes of the org chart's routes (``evo_agents.hub.server.org``): the chart as GET /v1/org answers it, and the
bodies of the writes. The keys of ``OrgUnitView`` and ``OrgSeatView`` are those `evo-agents hub org show --json`
prints (plan org-chart, criterion a4)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from evo_agents.hub import org

LOGIN_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$"  # evo_agents.hub.server.admin.LOGIN_NAME
HANDLE_PATTERN = r"^[a-z0-9][a-z0-9-]{0,99}$"  # evo_agents.hub.profiles.HANDLE_PATTERN
Slug = Annotated[str, StringConstraints(pattern=org.SLUG_PATTERN)]
LoginName = Annotated[str, StringConstraints(pattern=LOGIN_PATTERN)]
Handle = Annotated[str, StringConstraints(pattern=HANDLE_PATTERN)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=org.MAX_NAME_CHARS)]
Note = Annotated[str, StringConstraints(max_length=org.MAX_NOTE_CHARS)]
Level = Literal["A0", "A1", "A2", "A3", "A4", "A5"]
PLACE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$"  # an environment (host, <plugin>/<name>) or a worker's name
Place = Annotated[str, StringConstraints(pattern=PLACE_PATTERN)]
MAX_PLACES = 100  # environments or workers of one ceiling (schema 0110)
CLASSES_FIELD = "the class overrides, K1 to K12 each to deny, ask, notify or auto (with :condition where one fits)"
ChildKind = Literal["team", "cohort"]
Role = Literal["lead", "member", "intern", "agent"]


class OrgUnitView(BaseModel):
    slug: str
    name: str
    kind: Literal["org", "team", "cohort"]
    parent: str | None = Field(description="the slug of the unit above it; null for the root")
    ends_on: date | None = Field(description="the day a cohort ends; null for a team and the root")
    archived_at: datetime | None
    revision: int = Field(description="1 and up; every change of the unit or of a seat in it raises it")
    ceiling: Level | None = Field(description="the level its newest ceiling revision sets; null without one")
    effective: Level = Field(description="the lowest ceiling of the unit and every unit above it")
    source: str = Field(description="unit:<slug> that sets the effective level")


class OrgSeatView(BaseModel):
    id: int
    unit: str = Field(description="the slug of its unit")
    login: str | None = Field(description="the person holding it; null for an agent's seat")
    agent: str | None = Field(description="the handle of the agent profile holding it; null for a person's seat")
    project: str | None = Field(description="the project of the agent profile; null for a person's seat")
    role: Role
    primary: bool = Field(description="whether it decides the person's ceiling; an agent's seat always does")
    reports_to: str | None = Field(description="the login holding the seat it reports to")
    reports_to_seat: int | None
    sponsor: str | None = Field(description="the login answering for an agent's seat")
    starts_on: date
    ends_on: date | None
    ended_at: datetime | None
    ceiling: Level | None
    effective: Level = Field(description="A0 once the seat ended, else the lowest ceiling of it and its units")
    source: str = Field(description="unit:<slug> or seat:<id> that sets the effective level")


class OrgViewer(BaseModel):
    login: str
    admin: bool = Field(description="a hub admin, who may change everything")
    leads: list[str] = Field(description="the slugs of the units the viewer leads")
    effective: Level = Field(description="the viewer's own effective ceiling")
    source: str


class OrgChartView(BaseModel):
    units: list[OrgUnitView] = Field(description="every unit, the root first, then by slug")
    seats: list[OrgSeatView] = Field(description="every seat, live and ended, by unit and id")
    viewer: OrgViewer


class OrgRunCount(BaseModel):
    login: str = Field(description="the member who dispatched the runs, their owner")
    project: str
    running: int = Field(description="their runs in that project whose agent works now")


class OrgRuns(BaseModel):
    running: list[OrgRunCount] = Field(
        description="the runs at work the viewer sees, over the projects they hold a grant on, by login and project"
    )


class UnitCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: Slug
    parent: Slug
    kind: ChildKind
    name: Name | None = Field(None, description="the slug when left out")
    ends_on: date | None = Field(None, description="a cohort's last day")
    level: Level | None = Field(None, description="its ceiling; a cohort gets A1 when left out, a team none")
    note: Note | None = None


class UnitEdit(BaseModel):
    """The fields to change; a field left out keeps its value, ends_on null clears it."""

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    parent: Slug | None = None
    ends_on: date | None = None
    if_revision: int | None = Field(None, ge=1, description="409 unless the unit is at this revision now")


class SeatAdd(BaseModel):
    """A seat for a person (login) or for an agent profile (agent and project)."""

    model_config = ConfigDict(extra="forbid")

    unit: Slug
    role: Role
    login: LoginName | None = None
    agent: Handle | None = None
    project: Handle | None = None
    reports_to: LoginName | None = Field(None, description="the person the seat reports to; an intern's needs one")
    sponsor: LoginName | None = Field(None, description="the person answering for an agent's seat; it needs one")
    primary: bool | None = Field(
        None,
        description="whether the seat decides the person's ceiling; when left out it does if their primary seat now "
        "is at the root or they hold none",
    )
    starts_on: date | None = Field(None, description="today when left out")
    ends_on: date | None = None


class SeatEdit(BaseModel):
    """The fields to change; a field left out keeps its value, reports_to or ends_on null clears it."""

    model_config = ConfigDict(extra="forbid")

    role: Role | None = None
    primary: Literal[True] | None = Field(None, description="make it the person's primary seat")
    reports_to: LoginName | None = None
    sponsor: LoginName | None = None
    starts_on: date | None = None
    ends_on: date | None = None


class CeilingSet(BaseModel):
    """The next ceiling revision of one unit (unit), one person's primary seat (login) or one seat (seat)."""

    model_config = ConfigDict(extra="forbid")

    unit: Slug | None = None
    login: LoginName | None = None
    seat: int | None = Field(None, ge=1)
    level: Level
    note: Note | None = None
    classes: dict[str, str] = Field(default_factory=dict, max_length=12, description=CLASSES_FIELD)
    environments: list[Place] = Field(
        default_factory=list, max_length=MAX_PLACES, description="the environments it allows; empty: no limit"
    )
    workers: list[Place] = Field(
        default_factory=list, max_length=MAX_PLACES, description="the workers it allows; empty: no limit"
    )


class CeilingWritten(BaseModel):
    scope: Literal["unit", "seat"]
    target: str = Field(description="unit:<slug> or seat:<id>")
    revision: int
    level: Level
    previous: Level | None = Field(description="the level of the revision before; null for the first")
    note: str | None
    classes: dict[str, str] = Field(description=CLASSES_FIELD)
    environments: list[str] = Field(description="the environments it allows; empty: no limit")
    workers: list[str] = Field(description="the workers it allows; empty: no limit")
