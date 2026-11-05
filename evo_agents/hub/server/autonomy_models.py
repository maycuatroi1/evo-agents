"""The shapes of the autonomy routes (``evo_agents.hub.server.autonomy_routes``, plan autonomy-levels): the effective
autonomy GET /v1/autonomy answers, whose keys are those `evo-agents hub autonomy show --json` prints (criterion a3),
a person's own preference PUT /v1/autonomy/prefs writes (criterion a2), and the run reach of a person that GET and PUT
/v1/autonomy/run-reach read and write (``run_access_rules.REACH_GROUPS``)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from evo_agents.hub.server.org_models import CLASSES_FIELD, Level
from evo_agents.hub.server.run_access_rules import REACH_GROUPS

Mode = Literal["deny", "ask", "notify", "auto"]
ReachGroup = Literal[REACH_GROUPS]
REACH_FIELD = (
    "the groups a run's token (evr_...) of the person reaches besides what every run's token reaches: agents (a "
    "project's agent profiles and agent types), workers (workers, worker pools, join tokens), curator (the Curator's "
    "writes), releases (a project's release schedule and checking it now); empty: none, as for everyone who never chose"
)


class AutonomyClass(BaseModel):
    mode: Mode
    source: str = Field(
        description="the source that sets the mode: pref:<login>, seat:<id>, unit:<slug>, policy:<project>/<repo>, "
        "profile:<handle>, credential_share or instance"
    )
    detail: str = Field(description="how that source sets it, with the condition the mode acts under")


class AutonomyView(BaseModel):
    principal: str = Field(description="the login of the person whose autonomy it is")
    project: str | None
    agent: str | None = Field(description="the handle of the agent profile it runs as; null for the person alone")
    level: Level = Field(description="the lowest level the sources set")
    classes: dict[str, AutonomyClass] = Field(description="K1 to K12, each its effective mode")
    environments: list[str] | None = Field(description="the environments allowed; null: no limit")
    workers: list[str] | None = Field(description="the workers allowed; null: no limit")


class PrefSet(BaseModel):
    """The next revision of the caller's own preference: a level, class overrides merged onto those before, or a reset
    that drops them (to A5 without a level)."""

    model_config = ConfigDict(extra="forbid")

    level: Level | None = None
    classes: dict[str, str] = Field(default_factory=dict, max_length=12, description=CLASSES_FIELD)
    reset: bool = False


class PrefWritten(BaseModel):
    login: str
    revision: int
    level: Level
    previous: Level | None = Field(description="the level of the revision before; null for the first")
    classes: dict[str, str] = Field(description=CLASSES_FIELD)


class RunReach(BaseModel):
    login: str = Field(description="the person whose runs it is")
    groups: list[ReachGroup] = Field(description=REACH_FIELD)


class RunReachSet(BaseModel):
    """The whole run reach of the caller: it replaces the one before, and an empty list clears it."""

    model_config = ConfigDict(extra="forbid")

    groups: list[ReachGroup] = Field(max_length=len(REACH_GROUPS), description=REACH_FIELD)


class RunReachWritten(BaseModel):
    login: str = Field(description="the person whose runs it is")
    groups: list[ReachGroup] = Field(description=REACH_FIELD)
    previous: list[ReachGroup] = Field(description="the groups before this write")
