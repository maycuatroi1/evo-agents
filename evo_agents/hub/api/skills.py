"""The skills on the hub, ``/v1/skills``, which ``evo_agents.hub.skill_sync`` publishes and syncs, and the worker's
read of the global ones, ``/v1/worker/skills``, which a pool node's daemon asks instead
(``evo_agents.worker.skill_sync_loop``)."""

from __future__ import annotations

from typing import Any

from evo_agents.hub.api.resource import Resource
from evo_agents.hub.api.routes import route

WORKER_SKILLS = route("worker/skills")


class Skills(Resource):
    """``/v1/skills/<part>...?<query>``."""

    base = "skills"

    def __init__(self, hub: Any = None):
        super().__init__(hub)

    def skill(self, project: str | None, name: str) -> str:
        """``/v1/skills/global/<name>``, or ``/v1/skills/projects/<project>/<name>`` of a project's skill."""
        return self.path("global", name) if project is None else self.path("projects", project, name)
