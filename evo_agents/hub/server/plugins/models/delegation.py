"""What a delegation of a call to the records API is (``service.delegation``): its token's shape, its header, and
what a call resolves to."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from evo_agents.hub.plugin_records import Reach

PERMISSION = "records:use"
HEADER = "X-Evo-On-Behalf-Of"
PREFIX = "evd_"
CHAT_RUN = "chat"  # the kind of a chat run (``runs.kind``)
AGENT_SINK = "claude-code@anthropic"  # evo_agents.hub.memory.AGENT_SINK: where an agent without a sink reads through
SLACK = timedelta(seconds=60)


@dataclass(frozen=True)
class Resolved:
    """A call to the records API: what it reaches, and the role it writes with."""

    reach: Reach
    role: str | None
