"""What the Orchestrator of an agent system does in a thread the hub opened to it (``orchestrator``), beyond the frame of
``triage_actions``, and the route an agent escalates by (``docs/workers.md``, Escalation; ``docs/hub.md``, The
Orchestrator).

The routes take the worker token of the worker that holds the chat run; the hub runtime's tools call the same code
(``hub_tools``). The three actions are the Orchestrator's alone: the chat run is in a thread of ``orchestrator_threads``
(404 for any other) and its agent is the Orchestrator the agent of the thread escalates to (403 for any other):

- POST /v1/worker/runs/{id}/triage/answer: answer the thread. An escalation's wait is answered with the text, which
  wakes the asking run; a thread with no wait hands the text to the agent it is about, at the hop of the work.
- POST /v1/worker/runs/{id}/triage/reassign: give the work to another agent node of the system (never the Orchestrator,
  never the agent the thread is about, ``triage_rules.reassign_problem``), at the hop of the work: the work keeps its
  hop. An escalation's wait goes with it, so the end of that agent's run answers the asking run.
- POST /v1/worker/runs/{id}/triage/ask-owner: ask the person at the origin of the work a decision of category triage;
  the Orchestrator's run waits for it as a chat run's decision does, and still answers an escalation when it ends.

Each posts a ``Decision:`` line in the thread in the Orchestrator's name and writes an audit row
(``orchestrator.answer``, ``orchestrator.reassign``, ``orchestrator.ask_owner``); answer and reassign close the thread,
and a closed thread refuses both (409).

- POST /v1/worker/runs/{id}/escalate: the agent of a chat run asks the Orchestrator of its system
  (``orchestrator.escalate``); the run parks on its wait and holds no worker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, profiles, runs, system_context, tables, triage_rules
from evo_agents.hub.server import audit, orchestrator, run_waits, system_traces
from evo_agents.hub.server.agent_account_mask import mask_run, mask_values
from evo_agents.hub.server.chat_access import chat_access
from evo_agents.hub.server.chat_agents import REFUSALS, AgentRun, agent_run
from evo_agents.hub.server.chat_direct import direct_chat
from evo_agents.hub.server.chat_runs import Hand, agent_access, chat_run, hand_work, hub_reply, post_agent
from evo_agents.hub.server.chat_store import insert_message
from evo_agents.hub.server.decisions import DecisionAsk, OptionIn, store_decision
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.profile_store import by_handle
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.runs.service.plan_runs import held_plan_run
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})

REASON_CHARS = 600
CLOSING = ("answer", "reassign")  # the outcomes that close a thread
Reason = Annotated[str, Field(min_length=1, max_length=REASON_CHARS, description="why, for the Decision: line")]
Text = Annotated[str, Field(min_length=1, max_length=chats.MAX_MESSAGE_BYTES)]


class OrchestratorAnswerIn(BaseModel):
    text: Text = Field(description="the answer: it wakes the asking run, or goes to the agent of the thread")
    reason: Reason


class OrchestratorReassignIn(BaseModel):
    handle: str = Field(
        min_length=1, max_length=100, pattern=profiles.HANDLE_PATTERN, description="an agent node of the system"
    )
    note: Text = Field(description="the work as that agent should read it: it sees nothing of the thread")
    reason: Reason


class OrchestratorAskOwnerIn(BaseModel):
    question: str = Field(min_length=1, max_length=runs.MAX_QUESTION_CHARS)
    context: str | None = Field(None, min_length=1, max_length=runs.MAX_DECISION_CONTEXT_BYTES)
    options: list[OptionIn] = Field(min_length=runs.DECISION_OPTIONS[0], max_length=runs.DECISION_OPTIONS[1])
    reason: Reason


class OrchestratorActed(BaseModel):
    message_id: int = Field(description="the Decision: line the hub posted in the thread, in the Orchestrator's name")
    run_id: int | None = Field(description="the run the answer woke or the work went to; null for ask-owner")
    decision_id: int | None = Field(description="the decision asked of the owner; null for answer and reassign")


@dataclass(frozen=True)
class Frame:
    """What an action acts on: the Orchestrator's run, the thread's row, its lead and its system's spec."""

    run: AgentRun
    thread: object  # the row of orchestrator_threads, locked
    lead: str
    spec: dict


async def frame_of(conn: AsyncConnection, run: AgentRun) -> Frame:
    """The frame of chat run ``run`` in a thread opened to an Orchestrator: 404 for a run in no such thread, 403 for an
    agent that is not the Orchestrator the thread's agent escalates to, 409 for a thread an answer or a reassign
    closed."""
    found = await chat_run(conn, run.run_id) if run.kind == "chat" else None
    if found is None:
        raise HTTPException(404, f"run {run.run_id} is a {run.kind} run: only the agent of a chat run acts in a thread")
    thread = await orchestrator.thread_at(conn, found.chat_id, found.root_id, lock=True)
    if thread is None:
        raise HTTPException(404, f"chat run {run.run_id} is in no thread the hub opened to an Orchestrator")
    member = None if thread.subject_run_id is None else await system_traces.of_run(conn, thread.subject_run_id)
    lead = None if member is None else system_context.escalation_target(member.spec, thread.subject)
    if lead is None or run.profile.handle != lead:
        raise HTTPException(
            403, f"@{run.profile.handle} is not the Orchestrator of @{thread.subject}: only {lead or 'it'} acts here"
        )
    if thread.outcome in CLOSING:
        raise HTTPException(409, f"thread #{thread.id} is closed: its Orchestrator chose {thread.outcome} already")
    return Frame(run, thread, lead, member.spec)


async def _said(conn: AsyncConnection, frame: Frame, text: str) -> int:
    """The ``Decision:`` line of the thread, in the Orchestrator's name."""
    run, thread = frame.run, frame.thread
    chat = await agent_access(conn, thread.chat_id, run.profile, run.principal.user_id)
    if chat is None:
        raise HTTPException(409, f"@{run.profile.handle} is not in chat {thread.chat_id} any more")
    message_id, _ = await post_agent(conn, chat, run.profile, run.run_id, runs.clip(text, 4000), thread.root_message_id)
    return message_id


async def _audit(conn: AsyncConnection, frame: Frame, action: str, tail: str) -> None:
    run, thread = frame.run, frame.thread
    target = (
        f"system:{thread.system_id} thread:{thread.id} rule:{thread.rule} subject:@{thread.subject} "
        f"orchestrator_run:{run.run_id}{tail}"
    )
    await audit.record(
        conn,
        actor_id=run.principal.user_id,
        token_id=run.principal.token_id,
        action=action,
        target=target,
        project_id=thread.project_id,
    )


async def _close(conn: AsyncConnection, frame: Frame, outcome: str) -> None:
    t = tables.orchestrator_threads
    closed = func.now() if outcome in CLOSING else None
    await conn.execute(update(t).values(outcome=outcome, closed_at=closed).where(t.c.id == frame.thread.id))


async def _open_wait(conn: AsyncConnection, wait_id: int | None):
    """The open wait ``wait_id`` (state, project, plan); None when there is none or it was answered."""
    if wait_id is None:
        return None
    d = tables.decisions
    row = (await conn.execute(select(d.c.state, d.c.project_id, d.c.plan_id).where(d.c.id == wait_id))).one_or_none()
    return row if row is not None and row.state == "open" else None


async def _masked(conn: AsyncConnection, sealer, run: AgentRun, text: str) -> str:
    said = await mask_values(conn, sealer, run.profile.id, text)
    return await mask_run(conn, sealer, run.run_id, said)


async def _give(conn: AsyncConnection, frame: Frame, handle: str, text: str) -> tuple[int | None, str | None]:
    """(the run of agent ``handle`` the work went to, None), or (None, why none): a message of the Orchestrator in the
    direct chat of the person at the origin with that agent, handed at the hop of the work, in a savepoint."""
    run = frame.run
    target = await by_handle(conn, handle)
    if target is None or target.deleted_at is not None:
        return None, f"@{handle} is no agent the hub has"
    origin = run.principal
    nested = await conn.begin_nested()
    try:
        chat_id, _ = await direct_chat(conn, origin, handle)
        chat = await chat_access(conn, origin, chat_id)
    except HTTPException as exc:
        await nested.rollback()
        return None, str(exc.detail)
    message_id, _ = await insert_message(
        conn, chat, text, thread_root_id=None, author_profile_id=run.profile.id, run_id=run.run_id
    )
    hop = frame.thread.hop
    author = f"@{run.profile.handle}"
    hand = Hand(chat, message_id, message_id, target, origin.user_id, origin.login, run.via, hop, author, text)
    child, why = await hand_work(conn, hand)
    if child is None:
        await nested.rollback()
        return None, why
    await nested.commit()
    return child, None


def _work_text(frame: Frame, text: str) -> str:
    thread = frame.thread
    head = (
        f"@{frame.lead}, the Orchestrator, gives you work of thread #{thread.id} ({thread.rule} of @{thread.subject})"
    )
    return runs.clip(f"{head}:\n\n{text}", chats.MAX_MESSAGE_BYTES)


async def answer(conn: AsyncConnection, sealer, run: AgentRun, body: OrchestratorAnswerIn) -> OrchestratorActed:
    """Answer the thread of Orchestrator run ``run``: wake the escalation's wait with the text, or, with no wait, hand
    the text to the agent the thread is about; 409 when neither takes it."""
    frame = await frame_of(conn, run)
    thread = frame.thread
    text = await _masked(conn, sealer, run, body.text)
    wait = await _open_wait(conn, thread.wait_id)
    if wait is not None:
        said = f"Decision #{thread.wait_id} (wait) woke: @{frame.lead}, the Orchestrator you escalated to, answered:"
        woke = await run_waits.wake(
            conn, thread.wait_id, wait.project_id, wait.plan_id, runs.clip(f"{said}\n\n{text}", runs.MAX_ANSWER_BYTES)
        )
        if woke is None:
            raise HTTPException(409, f"run {thread.asker_run_id}, which escalated, takes no answer now")
        r = tables.runs
        await conn.execute(update(r).values(spawned_wait_id=None).where(r.c.spawned_wait_id == thread.wait_id))
        asker = await chat_run(conn, woke)
        if asker is not None:
            await hub_reply(conn, asker.chat_id, asker.root_id, runs.clip(f"@{frame.lead} answered:\n\n{text}", 2048))
        where = f"the wait #{thread.wait_id} of run #{thread.asker_run_id}, which woke as run #{woke}"
    else:
        woke, refusal = await _give(conn, frame, thread.subject, _work_text(frame, text))
        if refusal is not None:
            raise HTTPException(409, f"@{thread.subject} takes no answer: {refusal}")
        where = f"@{thread.subject} in run #{woke}"
    message_id = await _said(conn, frame, f"Decision: answered {where}: {body.reason}")
    await _audit(conn, frame, audit.ORCHESTRATOR_ANSWER, f" run:{woke}")
    await _close(conn, frame, "answer")
    log.info("orchestrator answered", extra={"thread_id": thread.id, "run_id": run.run_id, "woke": woke})
    return OrchestratorActed(message_id=message_id, run_id=woke, decision_id=None)


async def reassign(conn: AsyncConnection, sealer, run: AgentRun, body: OrchestratorReassignIn) -> OrchestratorActed:
    """Give the work of the thread of Orchestrator run ``run`` to agent ``body.handle`` of the system, at the hop of
    the work, with the escalation's wait when there is one; 409 for an agent the frame refuses."""
    frame = await frame_of(conn, run)
    thread = frame.thread
    problem = triage_rules.reassign_problem(
        system_context.node_kind(frame.spec, body.handle), body.handle, thread.subject
    )
    if problem is not None:
        raise HTTPException(409, problem)
    note = await _masked(conn, sealer, run, body.note)
    child, refusal = await _give(conn, frame, body.handle, _work_text(frame, note))
    if refusal is not None:
        raise HTTPException(409, f"@{body.handle} takes no work: {refusal}")
    if await _open_wait(conn, thread.wait_id) is not None:
        r = tables.runs
        await conn.execute(update(r).values(spawned_wait_id=None).where(r.c.spawned_wait_id == thread.wait_id))
        moved = {"spawned_by_run_id": thread.asker_run_id, "spawned_wait_id": thread.wait_id}
        await conn.execute(update(r).values(**moved).where(r.c.id == child))
    member = await system_traces.of_run(conn, run.run_id)
    if member is not None:
        await system_traces.record_tool(
            conn,
            member,
            system_context.TOOL_REASSIGN,
            to_handle=body.handle,
            reason=body.reason,
            attributes={"evo.child_run_id": child},
        )
    kept = f" with the wait #{thread.wait_id} of run #{thread.asker_run_id}" if thread.wait_id else ""
    text = f"Decision: gave the work of @{thread.subject} to @{body.handle} in run #{child}{kept}: {body.reason}"
    message_id = await _said(conn, frame, text)
    await _audit(conn, frame, audit.ORCHESTRATOR_REASSIGN, f" to:@{body.handle} run:{child}")
    await _close(conn, frame, "reassign")
    log.info("orchestrator reassigned", extra={"thread_id": thread.id, "run_id": run.run_id, "child": child})
    return OrchestratorActed(message_id=message_id, run_id=child, decision_id=None)


async def ask_owner(conn: AsyncConnection, run: AgentRun, body: OrchestratorAskOwnerIn, row) -> OrchestratorActed:
    """Ask the person at the origin of the work of the thread of Orchestrator run ``run`` (its locked ``row``, as
    ``held_plan_run`` reads it) a decision of category triage; the thread stays open for the answer or reassign that
    follows the owner's answer."""
    frame = await frame_of(conn, run)
    ask = DecisionAsk(
        category=runs.CHAT_DECISION_CATEGORY, question=body.question, context=body.context, options=body.options
    )
    view = await store_decision(conn, run.run_id, row, ask)
    text = f"Decision: asked the owner (decision #{view.id}): {body.question} Why: {body.reason}"
    message_id = await _said(conn, frame, text)
    await _audit(conn, frame, audit.ORCHESTRATOR_ASK_OWNER, f" decision:{view.id}")
    await _close(conn, frame, "ask_owner")
    log.info("orchestrator asked the owner", extra={"thread_id": frame.thread.id, "decision_id": view.id})
    return OrchestratorActed(message_id=message_id, run_id=None, decision_id=view.id)


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


@worker_router.post(
    "/runs/{run_id}/escalate", status_code=201, response_model=orchestrator.Escalated, responses=REFUSALS
)
async def escalate(
    request: Request, run_id: RunId, body: orchestrator.EscalateIn, user: CurrentUser
) -> orchestrator.Escalated:
    """Escalate a question to the Orchestrator of this chat run's system; the run parks on the wait the answer wakes."""
    async with request.app.state.engine.begin() as conn:
        run = await agent_run(conn, user, run_id)
        if run.kind != "chat":
            raise HTTPException(422, f"run {run_id} is a {run.kind} run: only a chat run's agent escalates")
        _, row = await held_plan_run(conn, user, run_id, lock=True, kinds=("chat",))
        return await orchestrator.escalate(conn, request.app.state.sealer, run, body, row)
