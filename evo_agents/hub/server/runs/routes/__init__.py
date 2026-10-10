"""The run API's routes, one APIRouter a section, put together in the order the API has always listed them: the
project's runs under /v1/projects, the worker's protocol under /v1/worker. A handler passes the request's parts to a
public function of ``evo_agents.hub.server.runs.service`` and answers with what it returns."""

from fastapi import APIRouter

from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.routes import controls, dispatching, listing, plan, reading, worker

router = APIRouter(prefix="/v1/projects", tags=["runs"], responses={401: {"model": ErrorBody}})
for section in (reading, listing, dispatching, controls):  # listing carries the stats, between its two routes
    router.include_router(section.router)

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
for section in (worker, plan):
    worker_router.include_router(section.router)
