"""The routes of the chat of an author run (``chats.service.author``): POST /v1/worker/runs/{id}/chat, GET
/v1/projects/{p}/runs/{id}/chat and POST /v1/projects/{p}/runs/{id}/finish."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Header, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.chats.models.author import ChatPost, ChatPosted, RunChat
from evo_agents.hub.server.chats.service import author as service
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.runs.models import REFUSALS, Run, RunId
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["runs"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


@worker_router.post(
    "/runs/{run_id}/chat",
    status_code=201,
    response_model=ChatPosted,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def post_chat(request: Request, run_id: RunId, body: ChatPost, user: CurrentUser) -> ChatPosted:
    """Keep the last message of a turn of the agent of an author run this worker holds as a message of its chat."""
    async with request.app.state.engine.begin() as conn:
        seq = await service.post_chat(conn, user, run_id, body.text)
    log.info("chat message of the agent", extra={"run_id": run_id, "seq": seq})
    return ChatPosted(run_id=run_id, seq=seq)


@router.get(
    "/{project}/runs/{run_id}/chat",
    response_model=RunChat,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def read_chat(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> RunChat:
    """The chat of an author run: the agent's messages and its owner's, oldest first, and whose turn it is."""
    async with request.app.state.engine.begin() as conn:
        return await service.read_chat(conn, user, project, run_id, sink)


@router.post("/{project}/runs/{run_id}/finish", response_model=Run, responses=REFUSALS)
async def finish(request: Request, project: ProjectName, run_id: RunId, user: CurrentUser) -> Run:
    """End the chat of an author run one dispatched: a parked one is done at once, and the worker holding one ends it
    done once its agent's turn is over."""
    async with request.app.state.engine.begin() as conn:
        view, ended = await service.finish(conn, user, project, run_id)
    if ended:
        log.info("author chat ended", extra={"run_id": run_id, "state": view.state, "login": user.login})
    return view
