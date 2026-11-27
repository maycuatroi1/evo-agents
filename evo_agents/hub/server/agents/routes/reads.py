"""The worker route of an agent's read of another project: POST /v1/worker/runs/{run}/agent/reads;
``evo_agents.hub.server.agents.service.reads`` says the rules."""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.agents.models.reads import CrossRead
from evo_agents.hub.server.agents.service import reads as service
from evo_agents.hub.server.agents.service.shapes import CrossReadIn
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.security import CurrentUser

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


@worker_router.post(
    "/runs/{run_id}/agent/reads",
    response_model=CrossRead,
    responses={code: {"model": ErrorBody} for code in (404, 409, 422, 502, 503)},
)
async def read(request: Request, run_id: RunId, body: CrossReadIn, user: CurrentUser) -> CrossRead:
    """Read the knowledge graph or the memories of another project, in a chat run this worker holds, within what the
    hub sink of the run's project lets in (see the module's docstring)."""
    return await service.read(request, user, run_id, body)
