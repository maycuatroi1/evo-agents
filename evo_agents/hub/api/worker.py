"""The worker protocol's routes on the hub (``/v1/worker/*``, docs/workers.md), which the daemon's
``evo_agents.worker.hubapi.WorkerHub`` asks with the worker token, and the routes of a run a member's CLI asks too.

- :class:`WorkerRoutes`: the worker's own routes, ``/v1/worker/<part>...`` (join, claim, heartbeat, guard/policy).
- :class:`WorkerRun`: the routes of one run the worker holds, ``/v1/worker/runs/<run id>/<part>...``.
- :class:`WorkerKg`: the graph routes a worker pushes a project's graph through, ``/v1/worker/kg/<project>``.
"""

from __future__ import annotations

from typing import Any

from evo_agents.hub.api.resource import Resource


class WorkerRoutes(Resource):
    base = "worker"


class WorkerRun(Resource):
    """The routes of run ``run_id``."""

    base = "worker/runs"

    def __init__(self, hub: Any, run_id: int):
        super().__init__(hub, int(run_id))
        self.run_id = int(run_id)


class WorkerKg(Resource):
    base = "worker/kg"
