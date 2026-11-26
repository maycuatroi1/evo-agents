"""Collections of a project the CLI's clients sync with the hub, and the routes of a plugin in a project.

- :class:`LocalRuns`: the runs of local sessions, ``/v1/projects/<project>/local-runs``.
- :class:`Transcripts`: the transcripts of the project's sessions, ``/v1/projects/<project>/transcripts``.
- :class:`Plans`: the project's plans, ``/v1/projects/<project>/plans``.
- :class:`Digests`: the digests of sessions, ``/v1/projects/<project>/digests``.
- :class:`ProjectPlugins`: the plugins enabled in a project, ``/v1/projects/<project>/plugins``.
- :class:`ProjectPluginRoutes`: the routes of plugin ``plugin`` in a project, ``/v1/x/<plugin>/<project>``.
"""

from __future__ import annotations

from typing import Any

from evo_agents.hub.api.resource import ProjectResource
from evo_agents.hub.api.routes import route


class LocalRuns(ProjectResource):
    collection = "local-runs"


class Transcripts(ProjectResource):
    collection = "transcripts"


class Plans(ProjectResource):
    collection = "plans"


class Digests(ProjectResource):
    collection = "digests"


class ProjectPlugins(ProjectResource):
    collection = "plugins"


class ProjectPluginRoutes(ProjectResource):
    """The routes of plugin ``plugin`` (a name known only when the CLI runs) in ``project``."""

    def __init__(self, hub: Any, plugin: str, project: str):
        super().__init__(hub, project)
        self.plugin = plugin

    @property
    def root(self) -> str:
        return route("x", self.plugin, self.project)
