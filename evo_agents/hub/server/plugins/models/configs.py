"""The shapes of a plugin's config as the hub keeps it (``service.configs``), and where a stored layer lives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from evo_agents.hub.server.credentials.models.sealing import plugin_config_aad
from evo_agents.plugins import config as plugin_config

MAX_FIELDS = 200  # fields one write sets or unsets
NO_KEY = (
    "this hub keeps no secrets: it needs EVO_HUB_SECRETS_KEY, the key that seals them; ask its operator to set it "
    "(docs/credentials.md); nothing changed"
)


class PluginConfigLayer(BaseModel):
    """One stored layer of a plugin's config at one version."""

    plugin: str
    layer: Literal[plugin_config.STORED_LAYERS]  # pyright: ignore[reportInvalidTypeForm]
    project: str | None
    agent: str | None = Field(description="the handle of the agent profile of an agent layer")
    version: int = Field(description="0 while it was never written")
    values: dict[str, Any] = Field(description="the plain values it sets")
    secrets: list[str] = Field(description="the secret fields it sets; the hub never shows their values")
    rolled_back_to: int | None = Field(description="the older version this one copies, for a rollback")
    changed_by: str | None
    changed_at: datetime | None


class PluginConfigWrite(BaseModel):
    """The next version of a layer: what ``values`` sets and ``unset`` clears; every other field stays."""

    model_config = ConfigDict(extra="forbid")

    values: dict[str, Any] = Field(
        default_factory=dict,
        max_length=MAX_FIELDS,
        description="field -> value; a secret field's value is a string the hub seals and never shows again",
    )
    unset: list[str] = Field(default_factory=list, max_length=MAX_FIELDS, description="fields the layer sets now")
    if_version: int | None = Field(None, ge=0, description="409 unless the layer is at this version now")


class PluginConfigRollback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1, description="the older version of the layer to copy as its next one")
    if_version: int | None = Field(None, ge=0, description="409 unless the layer is at this version now")


class ConfigField(BaseModel):
    name: str
    value: Any = Field(None, description="null for a secret, whose value the hub never shows")
    secret: bool
    layer: Literal[plugin_config.LAYERS] = Field(description="the highest layer that sets it")  # pyright: ignore[reportInvalidTypeForm]
    version: int | None = Field(description="that layer's version; null for the defaults and a run's own layer")


class EffectiveConfig(BaseModel):
    """The config a plugin gets in a project, for runs of an agent profile: each field from the highest layer."""

    plugin: str
    version: str = Field(description="the installed version, whose schema the layers are checked against")
    project: str
    agent: str = Field(description="the agent profile whose layer it includes")
    fields: list[ConfigField]
    problems: list[str] = Field(description="what does not fit the schema now, such as a required field none sets")
    digest: str = Field(description="sha256 of the effective config, a secret counted by its sealed value")


class RunPlugin(BaseModel):
    name: str
    version: str = Field(description="the version installed when a worker claimed the run")
    config_digest: str = Field(description="the digest of the run's effective config then")
    recorded_at: datetime


@dataclass(frozen=True)
class Place:
    """Where a stored layer lives: its plugin, its layer, and the project and agent profile it belongs to."""

    plugin_id: int
    plugin: str
    layer: str
    project_id: int | None = None
    project: str | None = None
    profile_id: int | None = None
    handle: str | None = None

    def aad(self, name: str) -> bytes:
        return plugin_config_aad(self.plugin_id, self.layer, self.project_id, self.profile_id, name)

    def target(self, version: int) -> str:
        where = {"instance": "", "project": f"{self.project}/", "agent": f"{self.project}/@{self.handle}/"}
        return f"{where[self.layer]}plugin:{self.plugin} config {self.layer} v{version}"
