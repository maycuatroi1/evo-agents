"""The route of group chat messages read whole by a run's agent: POST /v1/worker/runs/{id}/chat/read, behind
``evo-agents worker chat read`` (``chats.service.read``)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from evo_agents.hub.server.chats.models.read import READ_REFUSALS, AgentReadIn, ChatReading
from evo_agents.hub.server.chats.service.read import agent_read
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


@worker_router.post("/runs/{run_id}/chat/read", response_model=ChatReading, responses=READ_REFUSALS)
async def read_chat(request: Request, run_id: RunId, body: AgentReadIn, user: CurrentUser) -> ChatReading:
    """A message of a chat, or its thread, whole, as the agent of the run may read it (see the module's docstring)."""
    reading = await agent_read(request.app.state.engine, user, run_id, body.id, body.thread)
    log.info(
        "chat read by an agent", extra={"run_id": run_id, "chat_id": reading.chat_id, "read": len(reading.messages)}
    )
    return reading
