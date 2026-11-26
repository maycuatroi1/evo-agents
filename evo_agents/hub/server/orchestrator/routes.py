"""The worker protocol's routes of the Orchestrator (``docs/workers.md``, Escalation; ``docs/hub.md``, The
Orchestrator): its three actions in a thread the hub opened to it (``service.actions``) and an agent's escalation
(``service.threads.escalate``). Each takes the worker token of the worker that holds the chat run."""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.base.errors import Unprocessable
from evo_agents.hub.server.chats.models.agents import REFUSALS
from evo_agents.hub.server.chats.service.agents import agent_run
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.orchestrator.models import (
    Escalated,
    EscalateIn,
    OrchestratorActed,
    OrchestratorAnswerIn,
    OrchestratorAskOwnerIn,
    OrchestratorReassignIn,
)
from evo_agents.hub.server.orchestrator.service import threads as orchestrator
from evo_agents.hub.server.orchestrator.service.actions import answer, ask_owner, reassign
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.runs.service.plan_runs import held_plan_run
from evo_agents.hub.server.security import CurrentUser

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


@worker_router.post(
    "/runs/{run_id}/triage/answer", response_model=OrchestratorActed, responses={403: {"model": ErrorBody}, **REFUSALS}
)
async def answer_route(
    request: Request, run_id: RunId, body: OrchestratorAnswerIn, user: CurrentUser
) -> OrchestratorActed:
    """Answer the thread this Orchestrator's chat run is in (see the module's docstring)."""
    async with request.app.state.engine.begin() as conn:
        run = await agent_run(conn, user, run_id)
        return await answer(conn, request.app.state.sealer, run, body)


@worker_router.post(
    "/runs/{run_id}/triage/reassign",
    response_model=OrchestratorActed,
    responses={403: {"model": ErrorBody}, **REFUSALS},
)
async def reassign_route(
    request: Request, run_id: RunId, body: OrchestratorReassignIn, user: CurrentUser
) -> OrchestratorActed:
    """Give the work of the thread this Orchestrator's chat run is in to another agent of the system."""
    async with request.app.state.engine.begin() as conn:
        run = await agent_run(conn, user, run_id)
        return await reassign(conn, request.app.state.sealer, run, body)


@worker_router.post(
    "/runs/{run_id}/triage/ask-owner",
    response_model=OrchestratorActed,
    responses={403: {"model": ErrorBody}, **REFUSALS},
)
async def ask_owner_route(
    request: Request, run_id: RunId, body: OrchestratorAskOwnerIn, user: CurrentUser
) -> OrchestratorActed:
    """Ask the person at the origin of the thread's work a decision, as this Orchestrator's chat run."""
    async with request.app.state.engine.begin() as conn:
        run = await agent_run(conn, user, run_id)
        _, row = await held_plan_run(conn, user, run_id, lock=True, kinds=("chat",))
        return await ask_owner(conn, run, body, row)


@worker_router.post("/runs/{run_id}/escalate", status_code=201, response_model=Escalated, responses={**REFUSALS})
async def escalate(request: Request, run_id: RunId, body: EscalateIn, user: CurrentUser) -> Escalated:
    """Escalate a question to the Orchestrator of this chat run's system; the run parks on the wait the answer wakes."""
    async with request.app.state.engine.begin() as conn:
        run = await agent_run(conn, user, run_id)
        if run.kind != "chat":
            raise Unprocessable(f"run {run_id} is a {run.kind} run: only a chat run's agent escalates")
        _, row = await held_plan_run(conn, user, run_id, lock=True, kinds=("chat",))
        return await orchestrator.escalate(conn, request.app.state.sealer, run, body, row)
