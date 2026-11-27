"""The worker's route of the branch a release run pushed, POST /v1/worker/runs/{id}/release/branch
(``service.claims``)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.releases.models.claims import ReleaseBranch, ReleasePushed
from evo_agents.hub.server.releases.service import claims
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.security import CurrentUser

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}


@worker_router.post("/runs/{run_id}/release/branch", status_code=201, response_model=ReleaseBranch, responses=REFUSALS)  # pyright: ignore[reportArgumentType]
async def pushed(request: Request, run_id: RunId, body: ReleasePushed, user: CurrentUser) -> ReleaseBranch:
    """The worker pushed release/<version> of the release run's repo: kept on the run, for the hub to land once the
    run is done."""
    return await claims.push(request.app.state.engine, user, run_id, body)
