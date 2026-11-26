"""The routes the agent of a proposal's thread answers through, behind ``evo-agents worker curator answer`` and
``evo-agents worker curator merge`` (``service.threads`` says what each checks)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from evo_agents.hub.server.change_views import Change
from evo_agents.hub.server.curator.models.threads import CuratorAnswerIn, CuratorMergeIn
from evo_agents.hub.server.curator.service import threads
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.proposal_models import Proposal
from evo_agents.hub.server.runs.models import REFUSALS, RunId
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


# The answers carry the proposal's and the change's views, whose modules reach the service: they stay out of the models.


class CuratorAnswered(BaseModel):
    message_id: int = Field(description="the Decision: line the hub posted in the thread, in the agent's name")
    proposal: Proposal = Field(description="the proposal as answered, with the change accepting it made")


class CuratorMerged(BaseModel):
    message_id: int = Field(description="the Decision: line the hub posted in the thread, in the agent's name")
    change: Change = Field(description="the change as merged, with its merge commit")


@worker_router.post("/runs/{run_id}/curator/answer", response_model=CuratorAnswered, responses={**REFUSALS})
async def answer(request: Request, run_id: RunId, body: CuratorAnswerIn, user: CurrentUser) -> CuratorAnswered:
    """Accept, reject or defer the proposal of this thread for the chat run's dispatcher, an admin of the project, as
    their answer on the web does; 403 for another proposal or a dispatcher who is no admin, 409 for a proposal neither
    open nor deferred or a charter that has its admins answer."""
    async with request.app.state.engine.begin() as conn:
        proposal, message_id, proposal_id = await threads.answer(conn, user, run_id, body)
    log.info(
        "proposal answered by the agent",
        extra={"proposal_id": proposal_id, "action": body.action, "agent_run_id": run_id},
    )
    return CuratorAnswered(message_id=message_id, proposal=proposal)


@worker_router.post(
    "/runs/{run_id}/curator/merge",
    response_model=CuratorMerged,
    responses={**REFUSALS, 503: {"model": ErrorBody}},
)
async def merge(request: Request, run_id: RunId, body: CuratorMergeIn, user: CurrentUser) -> CuratorMerged:
    """Merge the pull request of a change of this thread's proposal that the hub left open, for the chat run's
    dispatcher, an admin of the project, at the head the Judge passed, with the workers' App: 403 for a change of
    another proposal, 409 with the reason for anything the merge does not allow, 503 while GitHub does not answer."""
    view, message_id = await threads.merge(request.app.state, user, run_id, body.change_id)
    log.info(
        "curator change merged by the agent",
        extra={"change_id": view.id, "sha": view.merge_sha, "agent_run_id": run_id},
    )
    return CuratorMerged(message_id=message_id, change=view)
