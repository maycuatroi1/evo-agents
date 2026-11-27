"""The worker's routes of a run's leases, POST and DELETE /v1/worker/runs/{id}/credentials (``service.leases``)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.credentials.models.leases import Credentials, GivenBack
from evo_agents.hub.server.credentials.service import leases
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.security import CurrentUser

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}


@worker_router.post("/runs/{run_id}/credentials", response_model=Credentials, responses=REFUSALS)  # pyright: ignore[reportArgumentType]
async def lease_credentials(request: Request, run_id: RunId, user: CurrentUser, response: Response) -> Credentials:
    """The leases of a run this worker holds: the owner's secrets for its project and a GitHub token for its repos on
    github.com, with the reason of each repo nothing covers."""
    found = await leases.lease(request.app, user, run_id, str(request.base_url).rstrip("/"))
    response.headers.update(NO_STORE)
    return found


@worker_router.delete("/runs/{run_id}/credentials", response_model=GivenBack, responses=REFUSALS)  # pyright: ignore[reportArgumentType]
async def give_back_credentials(request: Request, run_id: RunId, user: CurrentUser) -> GivenBack:
    """Give back every lease this worker holds of the run, in whatever state the run is; GitHub tokens are revoked."""
    return await leases.give_back(request.app, user, run_id)
