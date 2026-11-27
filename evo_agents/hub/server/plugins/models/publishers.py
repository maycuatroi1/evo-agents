"""The shapes of the publishers whose signed plugin bundles the hub accepts (``service.publishers``), and the refusals
of a trust."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field

from evo_agents.plugins.signing import PUBLISHER_ID

MAX_NOTE_CHARS = 500  # plugin_publishers.note (0154)
SIGNER = r"^(sigstore|ed25519):\S{1,600}$"
LOCKED = (
    "this hub's operator locked its publishers (EVO_HUB_PLUGIN_PUBLISHERS_LOCKED): the hub trusts only those "
    "EVO_HUB_PLUGIN_PUBLISHERS lists; ask the operator to add {publisher} there"
)
CORE = (
    "{publisher} is a core publisher of this hub, whose plugins run Python in the hub's processes: only the operator "
    "names what signs for it, in EVO_HUB_PLUGIN_PUBLISHERS"
)


class TrustedSigner(BaseModel):
    id: int = Field(description="the row to remove with DELETE /v1/plugin-publishers/{id}")
    signer: str = Field(description="sigstore:OWNER/REPO/.github/workflows/FILE.yml or ed25519:KEY, as it was trusted")
    identity: str = Field(description="how it reads among the publisher's identities")
    added_by: str | None
    added_at: datetime
    note: str | None = None


class PluginPublisher(BaseModel):
    id: str
    identities: list[str] = Field(
        description="what signs for it: sigstore:<workflow> for a CI workflow, ed25519:<key id> for a key"
    )
    core: bool = Field(description="whether its plugins may be core plugins, which run in the hub's processes")
    operator: bool = Field(False, description="whether EVO_HUB_PLUGIN_PUBLISHERS lists it")
    trusted: list[TrustedSigner] = Field(
        default_factory=list, description="the signers hub admins trusted for it, besides the operator's"
    )


class PublisherTrustIn(BaseModel):
    publisher: Annotated[str, Field(pattern=f"^{PUBLISHER_ID.pattern}$")]
    signer: Annotated[str, Field(pattern=SIGNER)]
    note: Annotated[str | None, Field(max_length=MAX_NOTE_CHARS)] = None
