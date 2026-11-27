"""Who a call of the plugin API acts as (``service.access``)."""

from __future__ import annotations

from dataclasses import dataclass

from evo_agents.hub.server.projects import ProjectAccess


@dataclass(frozen=True)
class Acting:
    """A call of plugin ``plugin`` (its id and name) in a project, in the name of ``login`` with ``access``."""

    plugin_id: int
    plugin: str
    user_id: int
    login: str
    access: ProjectAccess


__all__ = ["Acting"]
