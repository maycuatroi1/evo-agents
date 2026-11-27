"""What the Orchestrator of an agent system does in a thread the hub opened to it (``orchestrator``), beyond the frame of
``triage_actions``, and the route an agent escalates by (``docs/workers.md``, Escalation; ``docs/hub.md``, The
Orchestrator).

The routes (``routes``) take the worker token of the worker that holds the chat run; the hub runtime's tools call the same code
(``hub_tools``). The three actions are the Orchestrator's alone: the chat run is in a thread of ``orchestrator_threads``
(404 for any other) and its agent is the Orchestrator the agent of the thread escalates to (403 for any other):

- POST /v1/worker/runs/{id}/triage/answer: answer the thread. An escalation's wait is answered with the text, which
  wakes the asking run; a thread with no wait hands the text to the agent it is about, at the hop of the work.
- POST /v1/worker/runs/{id}/triage/reassign: give the work to another agent node of the system (never the Orchestrator,
  never the agent the thread is about, ``triage_rules.reassign_problem``), at the hop of the work: the work keeps its
  hop. An escalation's wait goes with it, so the end of that agent's run answers the asking run; so it is never an
  agent whose run waits on the asking run (``wait_cycles.upstream_handles``), which would close a cycle of waits.
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
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, runs, system_context, triage_rules
from evo_agents.hub.server import audit, run_waits, wait_cycles
from evo_agents.hub.server.agent_accounts.service.mask import mask_run, mask_values
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound
from evo_agents.hub.server.chats.service.agents import AgentRun
from evo_agents.hub.server.chats.service.runs import agent_access, chat_run, post_agent
from evo_agents.hub.server.decisions.models import DecisionAsk
from evo_agents.hub.server.decisions.service.asks import store_decision
from evo_agents.hub.server.orchestrator.models import (
    OrchestratorActed,
    OrchestratorAnswerIn,
    OrchestratorAskOwnerIn,
    OrchestratorReassignIn,
)
from evo_agents.hub.server.orchestrator.repository import actions as rows
from evo_agents.hub.server.orchestrator.service import threads as orchestrator
from evo_agents.hub.server.profile_store import by_handle
from evo_agents.hub.server.systems.service import traces as system_traces
from evo_agents.hub.server.work_chats import service as work_chats

log = logging.getLogger(__name__)

CLOSING = ("answer", "reassign")  # the outcomes that close a thread


@dataclass(frozen=True)
class Frame:
    """What an action acts on: the Orchestrator's run, the thread's row, its lead and its system's spec."""

    run: AgentRun
    thread: Any  # the row of orchestrator_threads, locked
    lead: str
    spec: dict


async def frame_of(conn: AsyncConnection, run: AgentRun) -> Frame:
    """The frame of chat run ``run`` in a thread opened to an Orchestrator: 404 for a run in no such thread, 403 for an
    agent that is not the Orchestrator the thread's agent escalates to, 409 for a thread an answer or a reassign
    closed."""
    found = await chat_run(conn, run.run_id) if run.kind == "chat" else None
    if found is None:
        raise NotFound(f"run {run.run_id} is a {run.kind} run: only the agent of a chat run acts in a thread")
    thread = await orchestrator.thread_at(conn, found.chat_id, found.root_id, lock=True)
    if thread is None:
        raise NotFound(f"chat run {run.run_id} is in no thread the hub opened to an Orchestrator")
    if thread.rule == triage_rules.OPS:
        return await _ops_frame(conn, run, thread)
    member = None if thread.subject_run_id is None else await system_traces.of_run(conn, thread.subject_run_id)
    lead = None if member is None else system_context.escalation_target(member.spec, thread.subject)
    if lead is None or run.profile.handle != lead:
        raise Forbidden(
            f"@{run.profile.handle} is not the Orchestrator of @{thread.subject}: only {lead or 'it'} acts here"
        )
    if thread.outcome in CLOSING:
        raise Conflict(f"thread #{thread.id} is closed: its Orchestrator chose {thread.outcome} already")
    assert member is not None  # a lead is read off a member alone
    return Frame(run, thread, lead, member.spec)


async def _ops_frame(conn: AsyncConnection, run: AgentRun, thread) -> Frame:
    """The frame of an ops thread (``ops_threads``): it is about the Orchestrator of the project's default system, the
    agent it names, which alone acts in it (403); a thread whose batch the hub settled takes no action (409)."""
    if run.profile.handle != thread.subject:
        raise Forbidden(
            f"@{run.profile.handle} is not the Orchestrator of ops thread #{thread.id}: only @{thread.subject} acts",
        )
    spec = await rows.system_spec(conn, thread.system_id)
    state = await rows.batch_state(conn, thread.id)
    if state != "running":
        raise Conflict(f"ops thread #{thread.id} is closed: the hub settled its batch")
    return Frame(run, thread, thread.subject, spec)


async def said(conn: AsyncConnection, frame: Frame, text: str) -> int:
    """The ``Decision:`` line of the thread, in the Orchestrator's name; the ops actions' too (``ops_actions``)."""
    run, thread = frame.run, frame.thread
    chat = await agent_access(conn, thread.chat_id, run.profile, run.principal.user_id)
    if chat is None:
        raise Conflict(f"@{run.profile.handle} is not in chat {thread.chat_id} any more")
    message_id, _ = await post_agent(conn, chat, run.profile, run.run_id, runs.clip(text, 4000), thread.root_message_id)
    return message_id


async def audited(conn: AsyncConnection, frame: Frame, action: str, tail: str) -> None:
    """The audit row of an action of ``frame``; the ops actions' too (``ops_actions``)."""
    run, thread = frame.run, frame.thread
    target = (
        f"system:{thread.system_id} thread:{thread.id} rule:{thread.rule} subject:@{thread.subject} "
        f"orchestrator_run:{run.run_id}{tail}"
    )
    await Auditor(conn, thread.project_id, run.principal).record(action, target)


async def _close(conn: AsyncConnection, frame: Frame, outcome: str) -> None:
    """Record ``outcome``; an answer or a reassign closes the thread, but an ops thread closes with its batch."""
    if frame.thread.rule == triage_rules.OPS and frame.thread.outcome == "ask_owner":
        outcome = "ask_owner"  # the decision of the batch stays recorded: one a batch
    close = outcome in CLOSING and frame.thread.rule != triage_rules.OPS
    await rows.set_outcome(conn, frame.thread.id, outcome, close=close)


async def _open_wait(conn: AsyncConnection, wait_id: int | None):
    """The open wait ``wait_id`` (state, project, plan); None when there is none or it was answered."""
    if wait_id is None:
        return None
    row = await rows.wait(conn, wait_id)
    return row if row is not None and row.state == "open" else None


async def _masked(conn: AsyncConnection, sealer, run: AgentRun, text: str) -> str:
    masked = await mask_values(conn, sealer, run.profile.id, text)
    return await mask_run(conn, sealer, run.run_id, masked)


async def _give(conn: AsyncConnection, frame: Frame, handle: str, text: str) -> tuple[int | None, str | None]:
    """(the run of agent ``handle`` the work went to, None), or (None, why none): a message of the Orchestrator handed
    at the hop of the work, in the work chat of the trace when it has one (``work_chats.hand_over``), else in the
    direct chat of the person at the origin with that agent, in a savepoint."""
    run = frame.run
    target = await by_handle(conn, handle)
    if target is None or target.deleted_at is not None:
        return None, f"@{handle} is no agent the hub has"
    asker = work_chats.Asker(run.profile, run.run_id, run.principal, run.via, frame.thread.hop)
    handed = await work_chats.hand_over(conn, asker, target, text)
    return handed.child, handed.why


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
    if thread.rule == triage_rules.OPS:
        raise Conflict("an ops thread takes no answer: act with the ops actions, reassign, or ask the owner")
    text = await _masked(conn, sealer, run, body.text)
    wait = await _open_wait(conn, thread.wait_id)
    if wait is not None:
        head = f"Decision #{thread.wait_id} (wait) woke: @{frame.lead}, the Orchestrator you escalated to, answered:"
        woke = await run_waits.wake(
            conn, thread.wait_id, wait.project_id, wait.plan_id, runs.clip(f"{head}\n\n{text}", runs.MAX_ANSWER_BYTES)
        )
        if woke is None:
            raise Conflict(f"run {thread.asker_run_id}, which escalated, takes no answer now")
        await rows.release_wait(conn, thread.wait_id)
        asker = await chat_run(conn, woke)
        if asker is not None:
            line = runs.clip(f"@{frame.lead} answered:\n\n{text}", 2048)
            await work_chats.say(conn, woke, (asker.chat_id, asker.root_id), line)
        where = f"the wait #{thread.wait_id} of run #{thread.asker_run_id}, which woke as run #{woke}"
    else:
        woke, refusal = await _give(conn, frame, thread.subject, _work_text(frame, text))
        if refusal is not None:
            raise Conflict(f"@{thread.subject} takes no answer: {refusal}")
        where = f"@{thread.subject} in run #{woke}"
    message_id = await said(conn, frame, f"Decision: answered {where}: {body.reason}")
    await audited(conn, frame, audit.ORCHESTRATOR_ANSWER, f" run:{woke}")
    await _close(conn, frame, "answer")
    log.info("orchestrator answered", extra={"thread_id": thread.id, "run_id": run.run_id, "woke": woke})
    return OrchestratorActed(message_id=message_id, run_id=woke, decision_id=None)


async def reassign(conn: AsyncConnection, sealer, run: AgentRun, body: OrchestratorReassignIn) -> OrchestratorActed:
    """Give the work of the thread of Orchestrator run ``run`` to agent ``body.handle`` of the system, at the hop of
    the work, with the escalation's wait when there is one; 409 for an agent the frame refuses."""
    frame = await frame_of(conn, run)
    thread = frame.thread
    asker, waiting = thread.asker_run_id, set()
    if asker is not None and await _open_wait(conn, thread.wait_id) is not None:
        waiting = await wait_cycles.upstream_handles(conn, asker)
    problem = triage_rules.reassign_problem(
        system_context.node_kind(frame.spec, body.handle), body.handle, thread.subject, waiting
    )
    if problem is not None:
        raise Conflict(problem)
    note = await _masked(conn, sealer, run, body.note)
    child, refusal = await _give(conn, frame, body.handle, _work_text(frame, note))
    if refusal is not None:
        raise Conflict(f"@{body.handle} takes no work: {refusal}")
    if await _open_wait(conn, thread.wait_id) is not None:
        await rows.move_wait(conn, thread.wait_id, thread.asker_run_id, child)
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
    message_id = await said(conn, frame, text)
    await audited(conn, frame, audit.ORCHESTRATOR_REASSIGN, f" to:@{body.handle} run:{child}")
    await _close(conn, frame, "reassign")
    log.info("orchestrator reassigned", extra={"thread_id": thread.id, "run_id": run.run_id, "child": child})
    return OrchestratorActed(message_id=message_id, run_id=child, decision_id=None)


async def ask_owner(conn: AsyncConnection, run: AgentRun, body: OrchestratorAskOwnerIn, row) -> OrchestratorActed:
    """Ask the person at the origin of the work of the thread of Orchestrator run ``run`` (its locked ``row``, as
    ``held_plan_run`` reads it) a decision of category triage; the thread stays open for the answer or reassign that
    follows the owner's answer."""
    frame = await frame_of(conn, run)
    if frame.thread.rule == triage_rules.OPS and frame.thread.outcome == "ask_owner":
        raise Conflict(f"ops thread #{frame.thread.id} asked the owner its one decision of the batch already")
    ask = DecisionAsk(
        category=runs.CHAT_DECISION_CATEGORY, question=body.question, context=body.context, options=body.options
    )
    view = await store_decision(conn, run.run_id, row, ask)
    text = f"Decision: asked the owner (decision #{view.id}): {body.question} Why: {body.reason}"
    message_id = await said(conn, frame, text)
    await audited(conn, frame, audit.ORCHESTRATOR_ASK_OWNER, f" decision:{view.id}")
    await _close(conn, frame, "ask_owner")
    log.info("orchestrator asked the owner", extra={"thread_id": frame.thread.id, "decision_id": view.id})
    return OrchestratorActed(message_id=message_id, run_id=None, decision_id=view.id)
