"""The routes of shared workers narrowed to units of the org chart, under /v1/org/workers (``service.shares``)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Request

from evo_agents.hub import org
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.server.workers.models.shares import WorkerShares
from evo_agents.hub.server.workers.models.workers import MAX_ID
from evo_agents.hub.server.workers.service import shares

WorkerId = Annotated[int, Path(ge=1, le=MAX_ID)]
UnitSlug = Annotated[str, Path(pattern=org.SLUG_PATTERN)]
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}

router = APIRouter(prefix="/v1/org/workers", tags=["org"], responses={401: {"model": ErrorBody}})


@router.get("", response_model=list[WorkerShares])
async def list_worker_shares(request: Request, user: CurrentUser) -> list[WorkerShares]:
    """The caller's live workers, every live worker for a hub admin, with whom each is shared."""
    async with request.app.state.engine.begin() as conn:
        return await shares.listed(conn, user)


@router.put("/{worker_id}/units/{slug}", response_model=WorkerShares, responses=REFUSALS)
async def share_worker(request: Request, worker_id: WorkerId, slug: UnitSlug, user: CurrentUser) -> WorkerShares:
    """Narrow the worker to a live unit of the org chart, sharing it when it was not."""
    shares.writer(user)
    async with request.app.state.engine.begin() as conn:
        return await shares.share(conn, user, worker_id, slug)


@router.delete("/{worker_id}/units/{slug}", response_model=WorkerShares, responses=REFUSALS)
async def unshare_worker(request: Request, worker_id: WorkerId, slug: UnitSlug, user: CurrentUser) -> WorkerShares:
    """Take a unit back from the worker; with its last unit, sharing ends too."""
    shares.writer(user)
    async with request.app.state.engine.begin() as conn:
        return await shares.unshare(conn, user, worker_id, slug)
