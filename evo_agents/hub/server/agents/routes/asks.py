"""The worker routes of an agent's asks of another agent: POST /v1/worker/runs/{run}/agent/asks and
.../agent/forwards; ``evo_agents.hub.server.agents.service.asks`` says the rules."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from evo_agents.hub.server.agents.models.asks import AgentAsked, AgentAskIn, AgentForwarded, AgentForwardIn
from evo_agents.hub.server.agents.service import asks as service
from evo_agents.hub.server.chats.models.agents import REFUSALS
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


@worker_router.post(
    "/runs/{run_id}/agent/asks",
    status_code=201,
    response_model=AgentAsked,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def ask(request: Request, run_id: RunId, body: AgentAskIn, user: CurrentUser) -> AgentAsked:
    """Ask agent ``handle`` of another project, in a chat run this worker holds (see the module's docstring)."""
    async with request.app.state.engine.begin() as conn:
        asked = await service.ask(conn, request.app.state.sealer, user, run_id, body)
    return asked


@worker_router.post(
    "/runs/{run_id}/agent/forwards",
    status_code=201,
    response_model=AgentForwarded,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def forward_answer(request: Request, run_id: RunId, body: AgentForwardIn, user: CurrentUser) -> AgentForwarded:
    """Forward the last answer of run ``body.run_id``, a run of the same trace of an agent system, verbatim into the
    thread of a chat run this worker holds (``forward``). A refusal is answered (201, ``refusal``) and said in the
    thread."""
    async with request.app.state.engine.begin() as conn:
        message_id, refusal = await service.forward_of(conn, user, run_id, body.run_id)
    log.info("answer forwarded", extra={"run_id": run_id, "forwarded": body.run_id, "refused": bool(refusal)})
    return AgentForwarded(message_id=message_id, refusal=refusal)
