"""The routes of the playbook (see the package): the delta of the agent of a review run, and the project's playbook
for its readers. Each opens the request's transaction and hands it to the service."""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Request

from evo_agents.hub import review
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.playbook import service
from evo_agents.hub.server.playbook.models import READ_REFUSALS, REFUSALS, PlaybookEntry, PlaybookView
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})


@worker_router.post("/runs/{run_id}/playbook", status_code=201, response_model=PlaybookEntry, responses=REFUSALS)
async def record_delta(
    request: Request, run_id: RunId, body: service.PlaybookDelta, user: CurrentUser
) -> PlaybookEntry:
    """One delta of the playbook from the agent of the review run this worker holds."""
    async with request.app.state.engine.begin() as conn:
        entry, entry_id = await service.record_delta(conn, request, user, run_id, body)
    log.info("playbook delta", extra={"run_id": run_id, "entry_id": entry_id, "op": body.op})
    return entry


@router.get("/{project}/curator/playbook", response_model=PlaybookView, responses=READ_REFUSALS)
async def show_playbook(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    lens: Annotated[
        Literal[tuple(review.LENSES)] | None,  # pyright: ignore[reportInvalidTypeForm]
        Query(description="its entries and those of no lens"),
    ] = None,
    retired: Annotated[bool, Query(description="the retired entries too, after the live ones")] = False,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> PlaybookView:
    """The project's playbook of the Curator, the most helpful first."""
    async with request.app.state.engine.begin() as conn:
        return await service.playbook_view(conn, user, project, sink, lens=lens, retired=retired)
