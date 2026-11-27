"""The limits of an agent profile's avatar, the path parameter and refusals of the profile routes, and the shapes of a
profile, its permissions and its versions (``evo_agents.hub.server.agents.service.profiles`` says the rules)."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Path
from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from evo_agents.hub import profiles
from evo_agents.hub.server.agents.models.packs import PinnedPack, Pins
from evo_agents.hub.server.base.models import StrictModel
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.signals.models import PRINTABLE  # the regex of projects.PRINTABLE, in a leaf module

AVATAR_KIND = "agent-avatar"
AVATAR_MAX_BYTES = 512 * 1024
AVATAR_TYPES = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WebP"}
UNAVAILABLE = "the blob store did not answer; the avatar was not changed, try again shortly"

Handle = Annotated[str, Path(pattern=profiles.HANDLE_PATTERN, description="the agent's handle, @ left out")]
READ_REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404)}
WRITE_REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
AVATAR_REFUSALS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorBody} for code in (403, 404, 409, 413, 422, 503)
}


class AgentAxis(StrictModel):
    """One axis of a profile's permissions: every value (all), or those of ``allow`` alone (allowlist)."""

    mode: Literal[profiles.MODES] = profiles.ALL  # pyright: ignore[reportInvalidTypeForm]
    allow: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=profiles.MAX_ALLOW, description="empty for mode all"
    )

    @model_validator(mode="after")
    def _no_list_for_all(self) -> AgentAxis:
        if self.mode == profiles.ALL and self.allow:
            raise ValueError("an axis of mode all takes no allow list")
        return self


class AgentPermissions(StrictModel):
    """What the runs of a profile may use, on eight axes; an axis left out allows all."""

    credential: AgentAxis = Field(
        default_factory=AgentAxis, description="secret names, and github-app for the App's token"
    )
    tool: AgentAxis = Field(default_factory=AgentAxis, description="read, edit, bash, web, agent, mcp:<server>")
    skill: AgentAxis = Field(default_factory=AgentAxis, description="skill names, global or the project's")
    worker: AgentAxis = Field(default_factory=AgentAxis, description="worker names, label:<label>, or pool:<name>")
    project: AgentAxis = Field(default_factory=AgentAxis, description="the other projects whose runs it may take")
    model: AgentAxis = Field(default_factory=AgentAxis, description="model names, as a runtime names them")
    runtime: AgentAxis = Field(
        default_factory=AgentAxis, description="claude-code, opencode, codex, and plugins' runtimes <plugin>/<name>"
    )
    environment: AgentAxis = Field(
        default_factory=AgentAxis,
        description="where the agent runs, in order of preference: host, the worker's own machine, and plugins' "
        "environments <plugin>/<name>, each a container; all runs on the host",
    )

    @model_validator(mode="after")
    def _known_items(self) -> AgentPermissions:
        for axis in profiles.AXES:
            rule: AgentAxis = getattr(self, axis)
            rule.allow = list(dict.fromkeys(profiles.normalized(axis, item) for item in rule.allow))
        found = profiles.problems(self.stored())
        if found:
            raise ValueError("; ".join(found))
        return self

    def stored(self) -> dict:
        """The permissions as the hub stores them: ``{"mode": "all"}``, or the mode with its allow list."""
        return {
            axis: {"mode": rule.mode, "allow": rule.allow} if rule.mode == profiles.ALLOWLIST else {"mode": rule.mode}
            for axis, rule in ((axis, getattr(self, axis)) for axis in profiles.AXES)
        }


DisplayName = Annotated[str, Field(min_length=1, max_length=profiles.MAX_NAME_CHARS, pattern=PRINTABLE)]
Description = Annotated[str, Field(max_length=profiles.MAX_DESCRIPTION_CHARS)]
Instructions = Annotated[str, Field(max_length=profiles.MAX_INSTRUCTIONS_CHARS)]
Color = Annotated[str, Field(pattern=profiles.COLOR_PATTERN, description="#rrggbb, lowercase")]
Starter = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=profiles.MAX_STARTER_CHARS, pattern=PRINTABLE),
]
Starters = Annotated[
    list[Starter],
    Field(
        max_length=profiles.MAX_STARTERS,
        description="questions the chat home offers before a first message to the agent, in order; one line each",
    ),
]

RunsOn = Annotated[
    Literal["worker", "hub"],
    Field(description="where its runs run: on a worker of its pool (worker) or on the hub (hub)"),
]


class AgentCreate(StrictModel):
    handle: str = Field(pattern=profiles.HANDLE_PATTERN, description="unique on the hub, no project's name; it stays")
    name: DisplayName
    description: Description = ""
    instructions: Instructions = Field("", description="added to the system prompt of the profile's runs")
    color: Color | None = Field(None, description="default: one picked by the handle")
    starters: Starters = Field(default_factory=list)
    runs_on: RunsOn | None = Field(None, description="default: worker")
    permissions: AgentPermissions = Field(default_factory=AgentPermissions)
    packs: Pins = Field(
        default_factory=list, description="the agent packs it attaches, in order; see GET .../agent-packs"
    )


class AgentEdit(StrictModel):
    """What to change; a field left out stays as it is."""

    name: DisplayName | None = None
    description: Description | None = None
    instructions: Instructions | None = None
    color: Color | None = None
    starters: Starters | None = Field(None, description="the starters whole, in order; [] for none")
    runs_on: RunsOn | None = None
    permissions: AgentPermissions | None = Field(None, description="the eight axes whole; an axis left out allows all")
    packs: Pins | None = Field(None, description="the agent packs it attaches, whole, in order; [] for none")
    if_version: int | None = Field(None, ge=1, description="409 unless the profile is at this version now")


class AvatarUpload(StrictModel):
    data: str = Field(
        min_length=1,
        max_length=(AVATAR_MAX_BYTES + 2) // 3 * 4,
        description=f"the image, base64: PNG, JPEG or WebP, at most {AVATAR_MAX_BYTES} bytes",
    )

    @field_validator("data")
    @classmethod
    def _base64(cls, value: str) -> str:
        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("data is not base64") from None
        return value


class Agent(BaseModel):
    """An agent profile, with field names close to an A2A Agent Card's (name, description, iconUrl)."""

    handle: str
    project: str
    name: str
    description: str
    instructions: str
    color: str
    icon_url: str | None = Field(description="GET of the avatar on this hub; null: draw the initial on the colour")
    avatar_sha256: str | None
    starters: list[str] = Field(description="questions the chat home offers before a first message, in order")
    runs_on: RunsOn
    is_default: bool = Field(description="the project's default profile, @<project>: every permission")
    permissions: AgentPermissions
    restricted: list[str] = Field(description="the axes that are allowlists, in the order of the eight")
    packs: list[PinnedPack] = Field(description="the agent packs it attaches, in order, each at the version it pins")
    home_pool: str | None = Field(description="the name of the agent's own pool, agent-<handle>; null for none")
    account_pool: str | None = Field(
        description="the name of the account pool its runs lease accounts from; null until an admin attaches one"
    )
    version: int
    created_by: str
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = Field(description="when it was deleted: it takes no new run, and reads as before")


class AgentVersion(BaseModel):
    version: int
    body: dict = Field(description="the profile as this version had it: handle, name, permissions and the rest")
    changed_by: str
    changed_at: datetime


class AccountPoolChoice(StrictModel):
    account_pool: str | None = Field(
        description="the name of an account pool of the project to attach; null detaches the agent's"
    )
