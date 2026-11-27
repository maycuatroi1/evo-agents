"""The agent of a run with a browser asks a person to sign in (plan agent-browser, criterion a8):
POST /v1/worker/runs/{run}/browser-login, which ``evo-agents worker browser login`` calls.

For a run that waits for its owner (RESUME_KINDS) it stores a decision of category ``login`` with the options done and
skip, whose context carries the reason, the page and the link to the live view of the run's browser, and notifies the
dispatcher in the Inbox and on Telegram as any decision does. The worker's daemon moves the run to waiting when the
agent's turn ends, keeping its slot and its browser; the answer reaches the agent through its inbox, and a Done also
makes the daemon save the browser's profile to the identity before the agent goes on.

A run that cannot wait (a step run) gets no decision: the identity is set needs_login, its owner is told, and the run
fails with the reason, so the person logs the identity in and dispatches it again.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.browser.models.asks import BrowserLoginAsked, BrowserLoginBody
from evo_agents.hub.server.browser.models.leases import REFUSALS
from evo_agents.hub.server.browser.service import asks as service
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.security import CurrentUser

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


@worker_router.post(
    "/runs/{run_id}/browser-login",
    status_code=201,
    response_model=BrowserLoginAsked,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def ask_login(request: Request, run_id: RunId, body: BrowserLoginBody, user: CurrentUser) -> BrowserLoginAsked:
    """Ask the dispatcher of a run with a browser to sign in on one of its identities, or, for a run that cannot wait,
    set the identity needs_login and fail the run."""
    service.check_url(body)
    return await service.ask_login(request.app.state.engine, user, run_id, body)
