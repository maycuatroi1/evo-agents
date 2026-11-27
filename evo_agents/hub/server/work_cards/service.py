"""What a work card does: its message posted once (``post_card``), the streams told when it changed (``refresh``,
``run_moved``, ``decision_moved``), the work set by the trace's coordinator (``set_work``, ``set_run_work``), read
(``shown``) and marked handled by a member (``mark_handled``). Each refusal is the package's (``models``); every query
is the repository's (``CardStore``)."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import chats
from evo_agents.hub.server import audit, work_status
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.chats.repository.store import write_event
from evo_agents.hub.server.chats.service.access import chat_access
from evo_agents.hub.server.chats.service.agents import AgentRun, agent_run
from evo_agents.hub.server.chats.service.runs import hub_reply
from evo_agents.hub.server.security import Principal, holds
from evo_agents.hub.server.systems.service import traces as system_traces
from evo_agents.hub.server.work_cards.models import (
    CARD_TEXT,
    COLUMNS,
    NO_WORK,
    NO_WORK_CHAT,
    NOT_COORDINATOR,
    NOT_WRITER,
    WorkSetIn,
)
from evo_agents.hub.server.work_cards.repository import STORE
from evo_agents.hub.server.work_chats import service as work_chats
from evo_agents.hub.server.work_status_models import WorkCard

log = logging.getLogger("evo_agents.hub.server.work_cards")  # the name the module had before it was a package


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
    await STORE.set_card(conn, trace.id, message_id, work_status.card_state(card))
    return message_id


async def refresh(conn: AsyncConnection, trace_id: int) -> bool:
    """Tell the streams of the root chat of trace ``trace_id`` that its card changed, when it did; whether it did."""
    row = await STORE.card(conn, trace_id)
    if row is None:
        return False
    card = (await work_status.cards(conn, [trace_id])).get(trace_id)
    state = None if card is None else work_status.card_state(card)
    if state is None or state == row.card_state:
        return False
    await STORE.set_state(conn, trace_id, state)
    await write_event(conn, row.chat_id, chats.MESSAGE_CHANGED, {"message_id": row.card_message_id})
    await system_traces.touch(conn, trace_id)  # the Observe of the system shows the work too
    return True


async def run_moved(conn: AsyncConnection, run_id: int) -> None:
    """Refresh the cards of the traces run ``run_id`` is in, once it moved or a decision of it changed."""
    for trace_id in await STORE.carded_traces(conn, run_id):
        await refresh(conn, trace_id)


async def decision_moved(conn: AsyncConnection, decision_id: int) -> None:
    """Refresh the cards of the traces of the run of decision ``decision_id``, once it was asked or answered."""
    run_id = await STORE.decision_run(conn, decision_id)
    if run_id is not None:
        await run_moved(conn, run_id)


# Setting the work


async def set_work(conn: AsyncConnection, run: AgentRun, body: WorkSetIn) -> WorkCard:
    """Set what ``body`` gives of the work of the trace of ``run``, its agent the coordinator (see the package's
    docstring); the card after it."""
    trace = await work_chats.trace_of_run(conn, run.run_id)
    if trace is None or trace.work_chat_id is None:
        raise HTTPException(409, NO_WORK.format(run=run.run_id))
    if run.profile.id != trace.coordinator_id:
        raise HTTPException(403, NOT_COORDINATOR.format(handle=run.profile.handle))
    given = body.model_dump(exclude_none=True)
    values = {COLUMNS.get(name, name): value for name, value in given.items()}
    if body.outcome is not None:  # a new outcome stands for itself: no mark of handled, no left of an older one
        values |= {"handled_by": None, "handled_at": None, "outcome_left": body.left}
    await STORE.set_work(conn, trace.id, values)
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


async def set_run_work(engine: AsyncEngine, user: Principal, run_id: int, body: WorkSetIn) -> WorkCard:
    """``set_work`` for run ``run_id``, which the worker of ``user`` holds, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        return await set_work(conn, await agent_run(conn, user, run_id), body)


# Reading the card and marking the work handled


async def shown(engine: AsyncEngine, user: Principal, chat_id: int) -> WorkCard:
    """The card of the trace whose work chat ``chat_id`` is, to member ``user``, who reads it."""
    async with UnitOfWork(engine) as conn:
        await chat_access(conn, user, chat_id)
        trace = await work_chats.trace_of_chat(conn, chat_id)
        card = None if trace is None else (await work_status.cards(conn, [trace.id])).get(trace.id)
        if card is None:
            raise HTTPException(404, NO_WORK_CHAT.format(chat=chat_id))
        return card


async def mark_handled(engine: AsyncEngine, user: Principal, chat_id: int) -> WorkCard:
    """Mark the work of the trace whose work chat ``chat_id`` is handled, as member ``user`` (see the package's
    docstring); the card after it."""
    async with UnitOfWork(engine) as conn:
        await chat_access(conn, user, chat_id)
        trace = await work_chats.trace_of_chat(conn, chat_id)
        if trace is None:
            raise HTTPException(404, NO_WORK_CHAT.format(chat=chat_id))
        project, role = await STORE.project_role(conn, trace.project_id, user.user_id)
        if role is None or not holds(role, "writer"):
            raise HTTPException(403, NOT_WRITER.format(chat=chat_id, project=project, role=role or "no grant"))
        await STORE.mark_handled(conn, trace.id, user.user_id)
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
