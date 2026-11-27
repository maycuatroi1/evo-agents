"""What the sync of the model gateway (``service.sync``) wants of it: its intervals, the name of a connection the hub
made, and what one provider account makes of the gateway."""

from __future__ import annotations

import re
from dataclasses import dataclass

INTERVAL = 60.0  # seconds between two rounds when nothing pokes
QUOTA_INTERVAL = 300.0  # seconds between two reads of the Z.ai accounts' quotas (provider_quota)
DEBOUNCE = 0.2  # seconds a poke waits for the writes that follow it
NAME = re.compile(r"evo-(\d+)-([0-9a-f]{16})")  # a connection the hub made
NODE_KINDS = {"custom-openai": "openai-compatible", "custom-anthropic": "anthropic-compatible"}


@dataclass(frozen=True)
class Wanted:
    """What one provider account makes of the gateway."""

    secret_id: int
    provider: str  # the catalog's id
    base_url: str
    api_format: str
    key: str
    active: bool
    fingerprint: str
    override: str | None = None  # the base URL the connection is told, when not the registry's (service.sync)

    @property
    def name(self) -> str:
        return f"evo-{self.secret_id}-{self.fingerprint}"

    @property
    def node_name(self) -> str:
        return f"evo-{self.secret_id}"

    @property
    def node_kind(self) -> str | None:
        return NODE_KINDS.get(self.provider)

    @property
    def api_type(self) -> str:
        return "responses" if self.api_format == "openai-responses" else "chat"
