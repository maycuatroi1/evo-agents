"""The shapes of the autonomy routes (``evo_agents.hub.server.autonomy_routes``, plan autonomy-levels): the effective
autonomy GET /v1/autonomy answers, whose keys are those `evo-agents hub autonomy show --json` prints (criterion a3), and
a person's own preference PUT /v1/autonomy/prefs writes (criterion a2)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from evo_agents.hub.server.org_models import CLASSES_FIELD, Level

Mode = Literal["deny", "ask", "notify", "auto"]


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
