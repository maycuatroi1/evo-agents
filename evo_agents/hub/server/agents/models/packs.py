"""The shapes of the agent packs a profile attaches: a pin, a pinned pack, and a pack a profile may attach
(``evo_agents.hub.server.agents.service.packs`` says the rules)."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from evo_agents.hub import profiles
from evo_agents.hub.server.base.models import StrictModel
from evo_agents.plugins import compat
from evo_agents.plugins.agent_packs import pack_name
from evo_agents.plugins.manifest import PLUGIN_ID
from evo_agents.plugins.runtimes import CONTRIBUTION

POINT = "agent_packs"
NOTHING = "nothing changed"
IMAGE_KEY = "image:"  # plugin_versions.r2_key of a core plugin of the hub's image: image:<entry point>


class AgentPackPin(StrictModel):
    """A pack a profile attaches: pack ``name`` of plugin ``plugin`` at version ``version`` of the plugin."""

    plugin: str = Field(pattern=PLUGIN_ID, description="the plugin's id")
    name: str = Field(pattern=f"^{CONTRIBUTION}$", description="the pack, as the plugin's manifest names it")
    version: str | None = Field(
        None,
        pattern=f"^{compat.PLUGIN_VERSION.pattern}$",
        description="a version of the plugin the hub holds; left out: the one installed now, which the profile pins",
    )

    @property
    def pack(self) -> str:
        return pack_name(self.plugin, self.name)


Pins = Annotated[list[AgentPackPin], Field(max_length=profiles.MAX_PACKS)]


class PinnedPack(BaseModel):
    """A pack a profile attaches, at the version of its plugin it pins."""

    pack: str = Field(description="<plugin>/<name>")
    plugin: str
    name: str
    version: str


def pinned_view(packs: list | None) -> list[PinnedPack]:
    """The packs of a profile (as it stores them) as its view shows them."""
    return [PinnedPack(pack=pack_name(item["plugin"], item["name"]), **item) for item in packs or ()]


class AgentPack(BaseModel):
    """A pack a profile of the project may attach: of a plugin the project enabled, at one of ``versions``."""

    pack: str = Field(description="<plugin>/<name>")
    plugin: str
    name: str
    title: str | None
    description: str | None
    installed_version: str = Field(description="the plugin's version installed now, which a pin without one takes")
    versions: list[str] = Field(
        description="the versions of the plugin the hub holds that declare the pack and run on this release, the "
        "newest first"
    )
