"""The refusals of reading the accounts of agents and the avatar link of an agent listed with them
(``evo_agents.hub.server.agent_accounts.service.views``)."""

from __future__ import annotations

from typing import Any

from evo_agents.hub.server.errors import ErrorBody

REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404)}


def icon_url(project: str, handle: str, avatar_sha256: str | None) -> str | None:
    """As ``profile_store.icon_url`` writes it."""
    if avatar_sha256 is None:
        return None
    return f"/v1/projects/{project}/agents/{handle}/avatar?v={avatar_sha256[:16]}"
