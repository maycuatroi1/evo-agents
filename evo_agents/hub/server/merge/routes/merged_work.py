"""The worker route POST /v1/worker/merged (``service.merged_work``): which ended runs of a worker had their work
merged."""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.merge.models.merged_work import MergedAnswer, MergedQuestion
from evo_agents.hub.server.merge.service import merged_work
from evo_agents.hub.server.security import CurrentUser

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


@worker_router.post("/merged", response_model=MergedAnswer, responses={403: {"model": ErrorBody}})
async def merged(request: Request, question: MergedQuestion, user: CurrentUser) -> MergedAnswer:
    """Of the ended runs the worker whose token asks still keeps, those whose work the hub knows merged."""
    ids = sorted(set(question.runs))
    app = getattr(request.app.state, "github_app", None)
    return await merged_work.merged(request.app.state.engine, app, user.token_id, ids)
