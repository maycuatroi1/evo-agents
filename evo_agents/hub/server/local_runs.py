"""Local runs: a Claude Code session on a member's own machine, which the hub watches and asks decisions for.

A local run (``runs.kind = 'local'``) is the record of one session the member's machine registers, so the web, the
Monitor and the run list show what that session does, and a question of its agent reaches the member wherever they
are (the Inbox, Telegram, the web) and the answer goes back to the session. The hub only watches it: it neither
dispatches nor steers it (a cancel, takeover, message or rerun of a local run is 409), no worker holds it, and it has
no lease, no slot and no attempts. It is its member's alone: no other member reads it in any list, log, stream or
decision, whatever their role, and each route below answers 404 to another one. The routes take the member's token
(a machine token from the machine, or a web session) and the writer role on the project.

``PUT /v1/projects/{project}/local-runs/{session_id}`` registers the session (the Claude session id is the key,
unique per project) and is idempotent: it answers 201 for a new run and 200 for one that exists, updates what the
machine says (machine, cwd, Claude account, title) and revives a run that ended. 403 when another member registered
that session id. ``POST .../local-runs/{id}/state`` reports ``running``, ``waiting``, ``idle`` or ``ended``, the plan
and step the session works on, and the title; every route below touches ``last_activity_at``. A run in no state but
``ended`` that sent nothing for ``runs.LOCAL_SILENT_SECONDS`` is ended by the reaper (``end_silent``), and an ended run
registers again with the same PUT. ``POST .../events`` stores the session's log as a worker's batch is stored
(``run_events.store_events``).

``POST .../decisions`` asks the member a decision, with the options of any decision and the category ``question`` too:
the run is ``waiting`` while one is open, the member is notified as for any decision, and answers it on the web, in
Telegram or with ``evo hub decision answer``. The first answer wins, as for every decision (409 for a later one). The
machine takes it by long-poll (``GET .../decisions/{id}?wait_s=N``, at most MAX_WAIT_SECONDS) or by asking again, or by
the run's inbox (``POST .../inbox``, as a worker's). When the person answered on the spot, in the session itself, the
machine reports it (``POST .../decisions/{id}/answered``): that is an answer like the others, and the first of them
wins.

A plan write that names the local run (``run_id`` of PUT and PATCH of ``/v1/projects/{project}/plans``) records it on
the revision (``plan_revisions.run_id``) and sets the run's plan, revision and step.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request, Response

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.decisions.models import AnswerIn, Decision
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.run_events import (
    BatchRoute,
    EventBatch,
    EventsAck,
    Inbox,
    InboxAck,
    InboxMessage,
    RunStreams,
    deliver,
    store_events,
    undelivered,
)
from evo_agents.hub.server.runs.models import LINE, MAX_ID, REFUSALS, Run, RunId
from evo_agents.hub.server.runs.models.local_runs import MAX_TITLE_CHARS as MAX_TITLE_CHARS
from evo_agents.hub.server.runs.models.local_runs import MAX_WAIT_SECONDS
from evo_agents.hub.server.runs.models.local_runs import POLL_SECONDS as POLL_SECONDS
from evo_agents.hub.server.runs.models.local_runs import LocalDecisionAsk as LocalDecisionAsk
from evo_agents.hub.server.runs.models.local_runs import LocalRegister as LocalRegister
from evo_agents.hub.server.runs.models.local_runs import LocalState as LocalState
from evo_agents.hub.server.runs.service import local_runs as service
from evo_agents.hub.server.runs.service.local_runs import (  # noqa: F401 (outputs, plans, run_reaper and the tests call them here)
    end_silent,
    note_progress,
    owned_local_run,
    writing_run,
)
from evo_agents.hub.server.security import CurrentUser

SESSION_ID = Annotated[str, Path(min_length=1, max_length=200, pattern=LINE, description="the Claude session id")]
DecisionId = Annotated[int, Path(ge=1, le=MAX_ID)]

REFUSED = {**REFUSALS, 422: {"model": ErrorBody}}
router = APIRouter(prefix="/v1/projects", tags=["local runs"], responses={401: {"model": ErrorBody}})
batch_router = APIRouter(
    prefix="/v1/projects", tags=["local runs"], route_class=BatchRoute, responses={401: {"model": ErrorBody}}
)


@router.put(
    "/{project}/local-runs/{session_id}",
    response_model=Run,
    status_code=200,
    responses={201: {"model": Run, "description": "the session was not registered yet"}, **REFUSED},
)
async def register(
    request: Request,
    response: Response,
    project: ProjectName,
    session_id: SESSION_ID,
    body: LocalRegister,
    user: CurrentUser,
) -> Run:
    """Register the Claude Code session ``session_id`` of the caller's machine, or say again that it is there: the
    run of that session, new (201) or the caller's own (200), whose machine, directory, account and title are the ones
    given and which runs again when it had ended."""
    view, created = await service.register(request.app.state.engine, user, project, session_id, body)
    if created:
        response.status_code = 201
    return view


@router.post(
    "/{project}/local-runs/{run_id}/state", response_model=Run, responses={**REFUSED, 409: {"model": ErrorBody}}
)
async def report_state(
    request: Request, project: ProjectName, run_id: RunId, body: LocalState, user: CurrentUser
) -> Run:
    """Report the state of the session, the plan and step it works on, and its title; every call is a sign of life.
    An ended run takes nothing but the PUT that registers the session again (409)."""
    return await service.report_state(request.app.state.engine, user, project, run_id, body)


@batch_router.post(
    "/{project}/local-runs/{run_id}/events",
    response_model=EventsAck,
    responses={**REFUSED, 413: {"model": ErrorBody}},
)
async def post_events(
    request: Request, project: ProjectName, run_id: RunId, body: EventBatch, user: CurrentUser
) -> EventsAck:
    """Store the events of the batch that follow the ones stored, and say up to where the machine's spool may go: as
    the worker's POST /v1/worker/runs/{id}/events does."""

    async def store(conn, row):
        return await store_events(request, conn, run_id, row.state, row.event_seq, row.events_acked, body.events)

    return await service.post_events(request.app.state.engine, user, project, run_id, store)


# Decisions


@router.post(
    "/{project}/local-runs/{run_id}/decisions",
    status_code=201,
    response_model=Decision,
    responses={**REFUSED, 409: {"model": ErrorBody}},
)
async def ask(request: Request, project: ProjectName, run_id: RunId, body: LocalDecisionAsk, user: CurrentUser):
    """Ask the member a decision and notify them: the run waits while one is open. 409 for a run that ended and past
    the open decisions a run may hold."""
    return await service.ask(request.app.state.engine, user, project, run_id, body)


@router.get(
    "/{project}/local-runs/{run_id}/decisions/{decision_id}",
    response_model=Decision,
    responses={**REFUSED},
)
async def take_decision(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    decision_id: DecisionId,
    user: CurrentUser,
    wait_s: Annotated[
        int,
        Query(
            ge=0,
            le=MAX_WAIT_SECONDS,
            description="hold the request up to this many seconds while the decision is open; 0 answers at once",
        ),
    ] = 0,
) -> Decision:
    """The decision as it is now: once it is answered, the answer is in it (``answer_option``, ``answer_text``), and
    the machine has taken it. With ``wait_s`` the request waits for the answer (long-poll) and holds no connection
    of the database meanwhile; ask again when it comes back open."""
    streams: RunStreams = request.app.state.run_streams
    return await service.take_decision(request.app.state.engine, streams, user, project, run_id, decision_id, wait_s)


@router.post(
    "/{project}/local-runs/{run_id}/decisions/{decision_id}/answered",
    response_model=Decision,
    responses={**REFUSED, 409: {"model": ErrorBody}},
)
async def answered_on_the_spot(
    request: Request, project: ProjectName, run_id: RunId, decision_id: DecisionId, body: AnswerIn, user: CurrentUser
) -> Decision:
    """Report that the person answered decision ``decision_id`` in the session itself: an answer like any other, so
    the first of the web, Telegram, ``hub decision answer`` and this wins, and a later one gets 409 with who got
    there first. Nothing goes to the session's inbox: it has the answer."""
    return await service.answered_on_the_spot(request.app.state.engine, user, project, run_id, decision_id, body)


@router.post("/{project}/local-runs/{run_id}/inbox", response_model=Inbox, responses=REFUSED)
async def take_inbox(
    request: Request, project: ProjectName, run_id: RunId, user: CurrentUser, body: InboxAck | None = None
) -> Inbox:
    """Mark the messages up to ``ack`` delivered, and hand out the ones that still wait: the answers to the run's
    decisions, as a worker's inbox hands them to its agent."""
    ack = (body or InboxAck()).ack
    rows = await service.take_inbox(request.app.state.engine, user, project, run_id, ack, deliver, undelivered)
    return Inbox(messages=[InboxMessage(**row._mapping) for row in rows])


__all__ = ["end_silent", "note_progress", "owned_local_run", "router", "batch_router", "writing_run"]
