"""The pool of an agent, as placement reads it (``evo_agents.hub.server.agents.service.placement``)."""

from __future__ import annotations

from dataclasses import dataclass

from evo_agents.hub.server.runs.models.placement import POOL_KEY

EMPTY_NOTICE = "agent_pool_empty"  # runs.NOTICE_KINDS


@dataclass(frozen=True)
class Home:
    """The pool of an agent: its id and name, and the taints its runs tolerate."""

    pool_id: int
    pool: str
    taints: tuple[str, ...]
    profile_id: int
    handle: str
    project_id: int

    @property
    def selector(self) -> str:
        return f"{POOL_KEY}={self.pool}"
