"""The routes of the work card: each one hands the request's parts and the app's engine to the service."""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.chats.models.agents import REFUSALS
from evo_agents.hub.server.chats.models.members import ChatId
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.server.work_cards import service
from evo_agents.hub.server.work_cards.models import WorkSetIn
from evo_agents.hub.server.work_status_models import WorkCard

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})


@worker_router.post("/runs/{run_id}/work", response_model=WorkCard, responses={403: {"model": ErrorBody}, **REFUSALS})
async def set_work_route(request: Request, run_id: RunId, body: WorkSetIn, user: CurrentUser) -> WorkCard:
    """Set the title, goal and done-when of the work of this run's trace, and record its outcome, as its
    coordinator."""
    return await service.set_run_work(request.app.state.engine, user, run_id, body)


@router.get("/{chat_id}/work", response_model=WorkCard, responses={code: {"model": ErrorBody} for code in (403, 404)})
async def show_work(request: Request, chat_id: ChatId, user: CurrentUser) -> WorkCard:
    """The work card of the trace whose work chat this is."""
    return await service.shown(request.app.state.engine, user, chat_id)


@router.post(
    "/{chat_id}/work/handled", response_model=WorkCard, responses={code: {"model": ErrorBody} for code in (403, 404)}
)
async def mark_handled_route(request: Request, chat_id: ChatId, user: CurrentUser) -> WorkCard:
    """Mark the work of the trace whose work chat this is handled (Mark as handled)."""
    return await service.mark_handled(request.app.state.engine, user, chat_id)
