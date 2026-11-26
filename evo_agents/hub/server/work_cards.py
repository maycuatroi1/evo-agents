"""The work card of a trace of an agent system (``docs/hub.md``, The work card; ``work_status`` derives it).

When a trace's work chat opens (``work_chats.open_work``), the hub posts one message of its own in the root thread, the
thread of the trace's first message: ``system_traces.card_message_id`` names it, and every read of it carries the card
as its ``work`` part (``chats.service.views.messages_view``). A move that changes what the card shows (a run of the trace moving,
``chats.service.runs.chat_run_moved``; a decision of one asked or answered, ``decisions``; the entry agent's ``work set``) writes
a ``message_changed`` event for it in the root chat, whose streams then read it again: a chat event, woken through
``evo_agents.hub.wake`` as every chat event is, so it needs no event bus. ``system_traces.card_state`` keeps the key of
what the card showed (``work_status.card_state``), so a move that changes nothing on it writes no event. A change of the
card, and every ``work set``, also moves the trace (``system_traces.touch``), so the trace stream of the system's
Observe sends it with its new title, status and wait.

- POST /v1/worker/runs/{id}/work (``evo-agents worker work set``, the hub runtime's ``work_set``): the coordinator (the
  system's entry agent) sets the title, goal and done-when of the work of its run's trace, and records its outcome
  (met or not_met against the done-when) with what is still to do. The worker token that holds the run; 409 for a run
  in no trace with a work chat, 403 for an agent that is not the coordinator. An outcome given clears a member's mark
  of handled, and what was left when ``left`` is not given with it.
- POST /v1/chats/{id}/work/handled (Mark as handled): a member of the work chat who holds the writer role on the
  trace's project marks the work handled, so a recorded not_met no longer reads so; the card names who and when. 404
  to anyone who does not read the chat and for a chat that is no work chat, 403 to a member who only reads (a reader
  of the project, or no grant on it), audited as ``work.handled``.
- GET /v1/chats/{id}/work: the card of the trace whose work chat that is, to a member who reads it (404 to anyone
  else, and for a chat that is no work chat; 403 to a member who no longer clears its label).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, system_context, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit, work_status
from evo_agents.hub.server.chats.models.agents import REFUSALS
from evo_agents.hub.server.chats.models.members import ChatId
from evo_agents.hub.server.chats.repository.store import write_event
from evo_agents.hub.server.chats.service.access import chat_access
from evo_agents.hub.server.chats.service.agents import AgentRun, agent_run
from evo_agents.hub.server.chats.service.runs import hub_reply
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.server.systems.service import traces as system_traces
from evo_agents.hub.server.work_chats import service as work_chats
from evo_agents.hub.server.work_status_models import Outcome, WorkCard

log = logging.getLogger(__name__)

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})

TITLE_CHARS = 200
CARD_TEXT = "Work card: {title}"
NO_WORK = (
    "run {run} is in no trace of an agent system with a work chat: `work set` names the work of a trace that handed "
    "work over"
)
NOT_COORDINATOR = "only the system's entry agent, which coordinates the trace, sets its work; @{handle} does not"


class WorkSetIn(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=TITLE_CHARS, description="a short name of the work")
    goal: str | None = Field(None, min_length=1, max_length=system_context.BRIEF_FIELD_CHARS)
    done_when: str | None = Field(None, min_length=1, max_length=system_context.BRIEF_FIELD_CHARS)
    outcome: Outcome | None = Field(
        None, description="met or not_met: whether the work reached its done-when, recorded before the final answer"
    )
    left: str | None = Field(
        None,
        min_length=1,
        max_length=system_context.BRIEF_FIELD_CHARS,
        description="what is still to do when the done-when is not met",
    )

    @model_validator(mode="after")
    def _something(self) -> WorkSetIn:
        if all(getattr(self, name) is None for name in FIELDS):
            raise ValueError(GIVE)
        return self


FIELDS = ("title", "goal", "done_when", "outcome", "left")
GIVE = "give --title, --goal, --done-when, --outcome or --left"
COLUMNS = {"left": "outcome_left"}  # a field of WorkSetIn: its column of system_traces, where the name differs
NOT_WRITER = (
    "marking the work of chat {chat} handled needs the writer role on project {project}, whose system's work it is; "
    "you hold {role}"
)


# The card's message


async def post_card(conn: AsyncConnection, trace: work_chats.Trace) -> int | None:
    """The hub's message carrying the card of ``trace`` in its root thread, posted once its work chat opened; its id,
    None for a trace whose first message went."""
    if trace.chat_id is None or trace.root_id is None:
        return None
    card = (await work_status.cards(conn, [trace.id])).get(trace.id)
    if card is None:
        return None
    message_id = await hub_reply(conn, trace.chat_id, trace.root_id, CARD_TEXT.format(title=card.title))
    t = tables.system_traces
    state = work_status.card_state(card)
    await conn.execute(update(t).values(card_message_id=message_id, card_state=state).where(t.c.id == trace.id))
    return message_id


async def refresh(conn: AsyncConnection, trace_id: int) -> bool:
    """Tell the streams of the root chat of trace ``trace_id`` that its card changed, when it did; whether it did."""
    t, m = tables.system_traces, tables.chat_messages
    query = (
        select(t.c.card_message_id, t.c.card_state, m.c.chat_id)
        .join(m, m.c.id == t.c.card_message_id)
        .where(t.c.id == trace_id)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return False
    card = (await work_status.cards(conn, [trace_id])).get(trace_id)
    state = None if card is None else work_status.card_state(card)
    if state is None or state == row.card_state:
        return False
    await conn.execute(update(t).values(card_state=state).where(t.c.id == trace_id))
    await write_event(conn, row.chat_id, chats.MESSAGE_CHANGED, {"message_id": row.card_message_id})
    await system_traces.touch(conn, trace_id)  # the Observe of the system shows the work too
    return True


async def _carded_traces(conn: AsyncConnection, run_id: int) -> list[int]:
    """The traces with a card that run ``run_id`` is a span of, or an Orchestrator's run on a thread of."""
    s, o, t = tables.system_spans, tables.orchestrator_threads, tables.system_traces
    spans = select(s.c.trace_id).where(s.c.run_id == run_id, s.c.name == system_context.INVOKE)
    threads = select(o.c.trace_id).where(o.c.orchestrator_run_id == run_id, o.c.trace_id.is_not(None))
    query = select(t.c.id).where(t.c.id.in_(spans.union(threads)), t.c.card_message_id.is_not(None))
    return list((await conn.execute(query)).scalars())


async def run_moved(conn: AsyncConnection, run_id: int) -> None:
    """Refresh the cards of the traces run ``run_id`` is in, once it moved or a decision of it changed."""
    for trace_id in await _carded_traces(conn, run_id):
        await refresh(conn, trace_id)


async def decision_moved(conn: AsyncConnection, decision_id: int) -> None:
    """Refresh the cards of the traces of the run of decision ``decision_id``, once it was asked or answered."""
    d = tables.decisions
    run_id = (await conn.execute(select(d.c.run_id).where(d.c.id == decision_id))).scalar_one_or_none()
    if run_id is not None:
        await run_moved(conn, run_id)


# Setting the work


async def set_work(conn: AsyncConnection, run: AgentRun, body: WorkSetIn) -> WorkCard:
    """Set what ``body`` gives of the work of the trace of ``run``, its agent the coordinator (see the module's
    docstring); the card after it."""
    trace = await work_chats.trace_of_run(conn, run.run_id)
    if trace is None or trace.work_chat_id is None:
        raise HTTPException(409, NO_WORK.format(run=run.run_id))
    if run.profile.id != trace.coordinator_id:
        raise HTTPException(403, NOT_COORDINATOR.format(handle=run.profile.handle))
    t = tables.system_traces
    given = body.model_dump(exclude_none=True)
    values = {COLUMNS.get(name, name): value for name, value in given.items()}
    if body.outcome is not None:  # a new outcome stands for itself: no mark of handled, no left of an older one
        values |= {"handled_by": None, "handled_at": None, "outcome_left": body.left}
    await conn.execute(update(t).values(**values).where(t.c.id == trace.id))
    await audit.record(
        conn,
        actor_id=run.principal.user_id,
        token_id=run.principal.token_id,
        action=audit.WORK_SET,
        target=f"trace:{trace.id} run:{run.run_id} " + " ".join(sorted(given)),
        project_id=trace.project_id,
    )
    if not await refresh(conn, trace.id):
        await system_traces.touch(conn, trace.id)
    log.info("work set", extra={"trace_id": trace.id, "run_id": run.run_id, "fields": sorted(given)})
    return (await work_status.cards(conn, [trace.id]))[trace.id]


# Marking the work handled


async def mark_handled(conn: AsyncConnection, user: Principal, chat_id: int) -> WorkCard:
    """Mark the work of the trace whose work chat ``chat_id`` is handled, as member ``user`` (see the module's
    docstring); the card after it."""
    await chat_access(conn, user, chat_id)
    trace = await work_chats.trace_of_chat(conn, chat_id)
    if trace is None:
        raise HTTPException(404, f"chat {chat_id} is no work chat")
    g, p = tables.grants, tables.projects
    query = (
        select(p.c.name, g.c.role)
        .select_from(p.outerjoin(g, (g.c.project_id == p.c.id) & (g.c.user_id == user.user_id)))
        .where(p.c.id == trace.project_id)
    )
    project, role = (await conn.execute(query)).one()
    if role is None or not has_role(role, "writer"):
        raise HTTPException(403, NOT_WRITER.format(chat=chat_id, project=project, role=role or "no grant"))
    t = tables.system_traces
    await conn.execute(update(t).values(handled_by=user.user_id, handled_at=func.now()).where(t.c.id == trace.id))
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.WORK_HANDLED,
        target=f"trace:{trace.id} chat:{chat_id}",
        project_id=trace.project_id,
    )
    if not await refresh(conn, trace.id):
        await system_traces.touch(conn, trace.id)
    log.info("work handled", extra={"trace_id": trace.id, "chat_id": chat_id})
    return (await work_status.cards(conn, [trace.id]))[trace.id]


@worker_router.post("/runs/{run_id}/work", response_model=WorkCard, responses={403: {"model": ErrorBody}, **REFUSALS})
async def set_work_route(request: Request, run_id: RunId, body: WorkSetIn, user: CurrentUser) -> WorkCard:
    """Set the title, goal and done-when of the work of this run's trace, and record its outcome, as its
    coordinator."""
    async with request.app.state.engine.begin() as conn:
        return await set_work(conn, await agent_run(conn, user, run_id), body)


@router.get("/{chat_id}/work", response_model=WorkCard, responses={code: {"model": ErrorBody} for code in (403, 404)})
async def show_work(request: Request, chat_id: ChatId, user: CurrentUser) -> WorkCard:
    """The work card of the trace whose work chat this is."""
    async with request.app.state.engine.begin() as conn:
        await chat_access(conn, user, chat_id)
        trace = await work_chats.trace_of_chat(conn, chat_id)
        card = None if trace is None else (await work_status.cards(conn, [trace.id])).get(trace.id)
        if card is None:
            raise HTTPException(404, f"chat {chat_id} is no work chat")
        return card


@router.post(
    "/{chat_id}/work/handled", response_model=WorkCard, responses={code: {"model": ErrorBody} for code in (403, 404)}
)
async def mark_handled_route(request: Request, chat_id: ChatId, user: CurrentUser) -> WorkCard:
    """Mark the work of the trace whose work chat this is handled (Mark as handled)."""
    async with request.app.state.engine.begin() as conn:
        return await mark_handled(conn, user, chat_id)
