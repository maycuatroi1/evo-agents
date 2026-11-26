"""The gateway to the hub's API that every client shares (docs/structure.md, section Hub API gateway).

Outside ``evo_agents.hub.server``, this package is the one place that writes a path of the hub's API (``/v1/...``):
the CLI, the worker and the graph's sync ask the hub through its resources, with the synchronous
``evo_agents.hub.client.Hub`` or the asynchronous ``evo_agents.worker.hubapi.WorkerHub`` alike. It imports neither the
server, the CLI nor the worker (contract hub-api-standalone).

- :mod:`.routes`: :func:`route` writes ``/v1/<base>/<part>...?<query>``.
- :mod:`.resource`: :class:`Resource` and its kinds, a collection of a project, of a feature, of a graph, or routes
  outside a project.
- :mod:`.worker`: the worker protocol's routes and a run's.
- :mod:`.paths`: the routes several modules ask (projects, auth, blobs, skills, workers).
"""

from evo_agents.hub.api.resource import (
    Client,
    FeatureResource,
    HubRoutes,
    KgResource,
    ProjectResource,
    Resource,
)
from evo_agents.hub.api.routes import V1, Query, is_route, query_string, quoted, route
from evo_agents.hub.api.worker import WorkerKg, WorkerRoutes, WorkerRun

__all__ = [
    "V1",
    "Client",
    "FeatureResource",
    "HubRoutes",
    "KgResource",
    "ProjectResource",
    "Query",
    "Resource",
    "WorkerKg",
    "WorkerRoutes",
    "WorkerRun",
    "is_route",
    "query_string",
    "quoted",
    "route",
]
