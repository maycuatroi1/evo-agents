"""The shapes of the hub's store of plugins (``service.store``) and the constants more than one layer gives."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Path
from pydantic import BaseModel, Field, model_validator

from evo_agents.plugins import signing
from evo_agents.plugins.bundle import MAX_BUNDLE
from evo_agents.plugins.manifest import PLUGIN_ID, TIERS, Manifest
from evo_agents.plugins.signing import MAX_SIGNATURE, SIGNATURE_KINDS

STATES = ("available", "installed", "disabled")
NOTICE = "plugin_disabled"  # evo_agents.hub.runs.NOTICE_KINDS
ADMIN_PAGE = "/admin/plugins"
MAX_REASON_CHARS = 2000  # plugins.disabled_reason (0034)
PLUGIN_NAME = PLUGIN_ID  # a manifest's id
VERSION = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]{1,40})?$"  # compat.PLUGIN_VERSION
UNAVAILABLE = "the blob store did not answer; nothing changed, try again shortly"
IMAGE = "image"  # plugin_versions.signature_kind of a core plugin that comes with the hub's image (0038)
FROM_IMAGE = (
    "plugin {name} is a core plugin of the hub's image: it is installed, upgraded and removed with the image, not "
    "through the store; nothing changed"
)
NOTHING_PUBLISHED = "nothing was published"
NOTHING_INSTALLED = "nothing was installed"
PluginName = Annotated[str, Path(pattern=PLUGIN_NAME, description="the plugin's id, as its manifest gives it")]


class PluginVersion(BaseModel):
    version: str
    sha256: str
    size: int
    publisher: str
    tier: Literal[TIERS]  # pyright: ignore[reportInvalidTypeForm]
    plugin_api: int
    engines: str = Field(description="the evo-agents versions it runs on, as its manifest gives them")
    permissions: list[str]
    signature_kind: Literal[(*SIGNATURE_KINDS, IMAGE)] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="image: a core plugin that comes with the hub's image, which nobody published or signed here"
    )
    signed_by: str = Field(
        description="the workflow identity of the Sigstore certificate, the Ed25519 key's id, or image:<entry point>"
    )
    compatible: bool = Field(description="whether it runs on this hub's release")
    incompatible_reason: str | None
    published_by: str | None = Field(description="null for a core plugin of the hub's image")
    published_at: datetime


class Plugin(BaseModel):
    name: str
    title: str = Field(description="the name its manifest gives it (the installed version's, else the latest's)")
    description: str
    tier: Literal[TIERS]  # pyright: ignore[reportInvalidTypeForm]
    publisher: str
    state: Literal[STATES] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="available: none installed; disabled: installed, turned off"
    )
    installed_version: str | None
    installed_by: str | None
    installed_at: datetime | None
    disabled_reason: str | None
    disabled_at: datetime | None
    latest_version: str
    upgrade_to: str | None = Field(description="the newest version above the installed one that runs on this hub")
    project: str | None = Field(
        None,
        description="the one project an admin of it installed the plugin for, which no other project sees or enables; "
        "null for a plugin the hub admins installed",
    )
    created_at: datetime


class PluginDetail(Plugin):
    manifest: dict = Field(description="the manifest of the installed version, or of the latest one")
    versions: list[PluginVersion] = Field(description="the newest first")


class PluginPublishIn(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="the bundle, committed to the blob store already")
    size: int = Field(ge=1, le=MAX_BUNDLE, description="bytes of the bundle")
    signature: dict | None = Field(
        description="the signature envelope beside the bundle (evo_agents.plugins.signing); null is refused, unsigned"
    )

    @model_validator(mode="after")
    def _bounded(self):
        if self.signature is not None and len(json.dumps(self.signature)) > MAX_SIGNATURE:
            raise ValueError(f"a signature is at most {MAX_SIGNATURE // 1024} KiB")
        return self


class PluginInstallIn(BaseModel):
    version: str | None = Field(None, pattern=VERSION, description="default: the newest that runs on this hub")


class PluginPublished(BaseModel):
    name: str
    created: bool = Field(description="false when the store holds this bundle as this version already")
    version: PluginVersion


@dataclass(frozen=True)
class Checked:
    """A bundle that passed every check, and who signed it."""

    manifest: Manifest
    signed: signing.Verified
