"""The Orchestrator of an agent system, as the triage of the agents it orchestrates (plan multi-agent-systems, step 5;
``docs/hub.md``, The Orchestrator): the thread the hub opens to it, with no model call, and the escalation an agent
asks of it.

An agent of a system that the system's Orchestrator orchestrates (an ``orchestrates`` edge to its node,
``system_context.lead_of``) has the Orchestrator for its triage: when its run or its turn fails (``on_turn_failed``
for a chat run, ``on_run`` from ``triage.on_failed`` and ``triage.on_stale`` for a run of ``triage.KINDS``), when it
escalates (``escalate``), and when a rule of progress matches its trace (``triage_rules``):

- loop: the agent received the same work ``triage_rules.LOOP_COUNT`` times in one trace, the same goal of a brief or
  the same normalized text (``check_loop``, when a run joins a trace and when an ask keeps its brief);
- idle: an open trace wrote no span for longer than its idle limit, ``limits.idle_minutes_hub`` for runs of the hub
  runtime and ``limits.idle_minutes_worker`` otherwise (``open_idle``, each pass of the reaper);
- rounds: a turn of the hub runtime used its tool rounds without a final answer (``on_rounds``);
- budget: the trace refused an ask or a mention for its budget (``on_budget``, when that refusal is written).

A failed or stale thread with no outcome closes, with none, once a later run of its agent in the same trace ended done
and the Orchestrator's run on it, if any, ended (``close_recovered``, each pass of the reaper): the work went on, so the
trace no longer shows failed for it.

The thread is in the work chat of the run's trace (``work_chats.placed``, which opens it), the Orchestrator and the
person at the origin of the work put in it; for a run of no trace, or an Orchestrator the work chat cannot take, it is
in the system's ops chat ("Ops: <slug>", ``agent_systems.ops_chat_id``), made the first time with the system's creator
in it, and a line of the hub in the work chat says where it went. Its first message is the
hub's: the rule, the agent, its run and trace, and the spans that show it, then the mention of the Orchestrator,
which hands it the work at the hop of the work it is about: an escalation adds no hop. The message carries the run it
is about, so the Orchestrator's run joins that run's trace under its span. ``orchestrator_threads`` keeps the thread;
its ``key`` makes a rule open one thread (one a failed run, a turn out of rounds, an idle or over-budget trace, an agent
in a loop of a trace), and an escalation opens one each time. A thread of a run of ``triage.KINDS`` is a triage thread
too (``run_triage``), so the Orchestrator has the frame of ``triage_actions`` there, and three actions beyond it
(``orchestrator_actions``): answer, reassign, ask the owner.

An agent no Orchestrator orchestrates keeps the triage it had, and an agent of no system the same. Each check runs in a
savepoint and logs what went wrong: a thread is a help and never undoes what it is about.
"""

from __future__ import annotations

import contextvars
import dataclasses
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from typing import cast

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import chats, runs, system_context, triage_rules
from evo_agents.hub.server import audit
from evo_agents.hub.server.agent_account_mask import mask_run, mask_values
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.chats.models.access import ChatAccess
from evo_agents.hub.server.chats.models.runs import Hand
from evo_agents.hub.server.chats.models.views import person_author
from evo_agents.hub.server.chats.repository.store import add_member, insert_mentions, member_event
from evo_agents.hub.server.chats.service.access import chat_access
from evo_agents.hub.server.chats.service.agents import AgentRun
from evo_agents.hub.server.chats.service.members import label_now, put_agent, put_person
from evo_agents.hub.server.chats.service.messages import resolve_mentions
from evo_agents.hub.server.chats.service.posting import insert_message
from evo_agents.hub.server.chats.service.runs import chat_run, hand_mentions, hub_reply
from evo_agents.hub.server.orchestrator.models import Escalated, EscalateIn
from evo_agents.hub.server.orchestrator.repository import threads as rows
from evo_agents.hub.server.profile_store import Profile, by_handle
from evo_agents.hub.server.run_state import RunStep
from evo_agents.hub.server.security import MACHINE, Principal
from evo_agents.hub.server.systems.service import traces as system_traces

log = logging.getLogger(__name__)

OPS_TITLE = "Ops: {slug}"
IDLE_BATCH = 50  # idle traces one pass of the reaper opens a thread for at most
EVIDENCE_SHOWN = 20  # span ids the first message names
ACTIONS_LINE = (
    "Act with `evo-agents worker triage answer|reassign|ask-owner` (the hub runtime's tools orchestrator_answer, "
    "orchestrator_reassign, orchestrator_ask_owner); each posts a `Decision:` line in this thread."
)
_OPENING = contextvars.ContextVar("orchestrator_opening", default=False)  # no rule checks while a thread opens


@dataclass(frozen=True)
class Opened:
    """A thread opened to the Orchestrator: its row, chat and first message, the Orchestrator's run (None when the
    hub could hand it no work, which the thread says)."""

    thread_id: int
    chat_id: int
    root_id: int
    run_id: int | None
    refusal: str | None = None
    kind: str = chats.GROUP  # chats.WORK for a thread in the work chat of its trace (``work_chats``)


@dataclass(frozen=True)
class Origin:
    """Who the work of a run is for, with the credential kind and the hop it is at."""

    person: Principal
    via: str
    hop: int


async def _person(conn: AsyncConnection, user_id: int) -> Principal:
    login = await rows.login(conn, user_id)
    # no token (None), as the hub's other machine principals: Principal types token_id int
    return Principal(user_id, login, False, cast(int, None), MACHINE, "")


async def machine_person(conn: AsyncConnection, user_id: int) -> Principal:
    """Person ``user_id`` as the hub acts for them: a machine principal of no token."""
    return await _person(conn, user_id)


async def origin_of(conn: AsyncConnection, run_id: int) -> Origin:
    """The person at the origin of the work of run ``run_id`` (for a chat run, the person at the origin of its chain),
    its credential kind and its hop."""
    found = await chat_run(conn, run_id)
    if found is not None:
        return Origin(await _person(conn, found.dispatcher_id), found.via, found.hop)
    row = await rows.run_origin(conn, run_id, MACHINE)
    return Origin(await _person(conn, row[0]), row[1], int(row[2]))


def subject_lead(found: system_traces.Membership | None) -> str | None:
    """The Orchestrator of the agent of ``found``; None for a run of no system, of no node, or no Orchestrator's."""
    if found is None or found.node is None:
        return None
    return system_context.lead_of(found.spec, found.handle)


# The ops chat


async def _made(conn: AsyncConnection, system: system_traces.SystemRef, creator: Principal) -> int:
    title = OPS_TITLE.format(slug=system.slug)[: chats.MAX_TITLE_CHARS]
    chat_id = await rows.add_chat(conn, title, creator.user_id)
    await add_member(conn, chat_id, user_id=creator.user_id, added_by=creator.user_id)
    await member_event(conn, chat_id, "member_added", person_author(creator.login), creator.login)
    tokenless = dataclasses.replace(creator, token_id=cast(int, None))  # the chat is the hub's: no token made it
    await Auditor(conn, system.project_id, tokenless).record(audit.CHAT_CREATE, f"chat:{chat_id}")
    return chat_id


async def ops_chat(
    conn: AsyncConnection, system: system_traces.SystemRef, person: Principal, lead: Profile
) -> ChatAccess:
    """The ops chat of ``system`` as ``person`` sees it: made the first time, the system's creator its creator (else
    ``person``); the Orchestrator ``lead`` and ``person`` put in it, as the group chat's rules let them."""
    from evo_agents.hub.server.triage import as_member  # triage opens these threads through this module

    row = await rows.ops_row(conn, system.id)
    chat_id = row.ops_chat_id
    if chat_id is None:
        creator = person if row.created_by is None else await _person(conn, row.created_by)
        chat_id = await _made(conn, system, creator)
        await rows.set_ops_chat(conn, system.id, chat_id)
    chat, member = await as_member(conn, chat_id)
    if await put_agent(conn, member, chat, lead):
        chat = await label_now(conn, chat)
    await put_person(conn, member, chat, person.user_id, person.login)
    return await chat_access(conn, person, chat_id)


# Opening a thread


@dataclass(frozen=True)
class Ask:
    """What a thread is about: the agent's run and membership, the rule, its key and evidence, the lines of the first
    message, and for an escalation the run that asked."""

    found: system_traces.Membership
    lead: str
    rule: str
    lines: list[str]
    key: str | None = None
    evidence: tuple[int, ...] = ()
    asker_run_id: int | None = None


def _head(ask: Ask) -> list[str]:
    found = ask.found
    run = f" (run #{found.run_id})" if found.run_id is not None else ""
    trace = f" in trace #{found.trace_id}" if found.trace_id is not None else ""
    shown = ", ".join(f"span:{span}" for span in ask.evidence[:EVIDENCE_SHOWN])
    more = len(ask.evidence) - EVIDENCE_SHOWN
    lines = [
        f"Rule: {ask.rule}: {triage_rules.RULE_TEXT[ask.rule]}.",
        f"Agent: @{found.handle}{run}{trace} of system {found.system.slug}.",
    ]
    if shown:
        lines.append(f"Evidence: {shown}" + (f" and {more} more" if more > 0 else "") + ".")
    return lines


async def _thread_row(conn: AsyncConnection, ask: Ask, hop: int) -> int | None:
    values = {
        "system_id": ask.found.system.id,
        "project_id": ask.found.system.project_id,
        "trace_id": ask.found.trace_id,
        "subject": ask.found.handle,
        "subject_run_id": ask.found.run_id,
        "rule": ask.rule,
        "key": ask.key,
        "evidence": list(ask.evidence),
        "asker_run_id": ask.asker_run_id,
        "hop": hop,
    }
    return await rows.add_thread(conn, values)


async def _hand(conn: AsyncConnection, chat: ChatAccess, lead: Profile, origin: Origin, message_id: int, body: str):
    """(the Orchestrator's run, the refusal) of the mention of ``lead`` in message ``message_id``, at the hop of the
    work; a refusal is said in the thread."""
    kept, _, _ = await resolve_mentions(conn, chat, origin.person, [lead.handle])
    person = origin.person
    hand = Hand(
        chat, message_id, message_id, lead, person.user_id, person.login, origin.via, origin.hop, "the hub", body
    )
    refusals = await hand_mentions(conn, hand, kept)
    await insert_mentions(conn, message_id, kept)
    refusals += [mention["refusal"] for mention in kept if mention["refusal"] and mention["refusal"] not in refusals]
    if refusals:
        await hub_reply(conn, chat.chat_id, message_id, "\n".join(refusals))
    run_id = next((mention.get("run_id") for mention in kept if mention.get("run_id")), None)
    return run_id, ("\n".join(refusals) or None)


async def open_thread(conn: AsyncConnection, ask: Ask) -> Opened | None:
    """Open the thread ``ask`` names in the ops chat of its system, the caller holding a savepoint to undo it; None
    when its key has a thread already, or the Orchestrator is no profile the hub has."""
    lead = await by_handle(conn, ask.lead)
    if lead is None or lead.deleted_at is not None or ask.found.system.id is None or ask.found.run_id is None:
        return None
    origin = await origin_of(conn, ask.found.run_id)
    thread_id = await _thread_row(conn, ask, origin.hop)
    if thread_id is None:
        return None
    token = _OPENING.set(True)
    try:
        chat = await _thread_chat(conn, ask, origin.person, lead)
        text = runs.clip("\n".join([*_head(ask), *ask.lines, ACTIONS_LINE]), chats.MAX_MESSAGE_BYTES - 200)
        body = f"{text}\n\n@{lead.handle}"
        message_id, _ = await insert_message(conn, chat, body, thread_root_id=None, run_id=ask.found.run_id)
        run_id, refusal = await _hand(conn, chat, lead, origin, message_id, body)
    finally:
        _OPENING.reset(token)
    await rows.placed(conn, thread_id, chat.chat_id, message_id, run_id)
    log.info(
        "orchestrator thread opened",
        extra={"rule": ask.rule, "subject_run_id": ask.found.run_id, "thread_id": thread_id, "run_id": run_id},
    )
    return Opened(thread_id, chat.chat_id, message_id, run_id, refusal, chat.kind)


async def open_ops(
    conn: AsyncConnection, system: system_traces.SystemRef, lead: Profile, person: Principal, key: str, text: str
) -> Opened | None:
    """Open the ops thread ``key`` of ``system`` (``ops_threads``: a batch of the ops queue) to its Orchestrator
    ``lead`` in the system's ops chat, for ``person`` at hop 1: a thread of rule ops about the Orchestrator itself,
    of no run, whose first message is ``text`` and mentions ``lead``. None when the key has a thread already; the
    caller holds a savepoint to undo it."""
    values = {
        "system_id": system.id,
        "project_id": system.project_id,
        "subject": lead.handle,
        "rule": triage_rules.OPS,
        "key": key,
        "evidence": [],
        "hop": 1,
    }
    thread_id = await rows.add_thread(conn, values)
    if thread_id is None:
        return None
    token = _OPENING.set(True)
    try:
        chat = await ops_chat(conn, system, person, lead)
        body = f"{runs.clip(text, chats.MAX_MESSAGE_BYTES - 200)}\n\n@{lead.handle}"
        message_id, _ = await insert_message(conn, chat, body, thread_root_id=None)
        run_id, refusal = await _hand(conn, chat, lead, Origin(person, MACHINE, 1), message_id, body)
    finally:
        _OPENING.reset(token)
    await rows.placed(conn, thread_id, chat.chat_id, message_id, run_id)
    log.info("ops thread opened", extra={"key": key, "thread_id": thread_id, "run_id": run_id})
    return Opened(thread_id, chat.chat_id, message_id, run_id, refusal, chat.kind)


async def _thread_chat(conn: AsyncConnection, ask: Ask, person: Principal, lead: Profile) -> ChatAccess:
    """The chat a thread of ``ask`` opens in: the work chat of its trace (``work_chats``), else the ops chat of its
    system, which a line in the work chat names when the Orchestrator may not be in it."""
    from evo_agents.hub.server.work_chats import (
        service as work_chats,  # it hands work through chat_runs, as this module does
    )

    trace = None
    if ask.found.trace_id is not None and ask.found.run_id is not None:
        trace = await work_chats.trace_of_run(conn, ask.found.run_id)
    if trace is None or trace.system_id is None:
        return await ops_chat(conn, ask.found.system, person, lead)
    work, chat, why = await work_chats.placed(conn, trace, person, lead)
    if chat is not None:
        return await chat_access(conn, person, work)
    ops = await ops_chat(conn, ask.found.system, person, lead)
    line = f"The thread of @{lead.handle} on @{ask.found.handle} ({ask.rule}) is in the ops chat {ops.chat_id}: {why}"
    await hub_reply(conn, work, None, line)
    return ops


async def _safely(conn: AsyncConnection, rule: str, make: Callable[[], Awaitable[Opened | None]]) -> Opened | None:
    """``make()`` in a savepoint; None, logged, when it raises; None at once while a thread opens."""
    if _OPENING.get():
        return None
    try:
        async with conn.begin_nested():
            return await make()
    except Exception as exc:  # a thread is a help: nothing inside it may undo what it is about
        log.warning("orchestrator thread not opened", extra={"rule": rule, "error": f"{type(exc).__name__}: {exc}"})
        return None


# Failed and stale runs


async def _evidence(conn: AsyncConnection, run_id: int) -> tuple[int, ...]:
    return await rows.evidence(conn, run_id)


async def on_turn_failed(conn: AsyncConnection, run_id: int) -> Opened | None:
    """The thread of chat run ``run_id`` of an agent an Orchestrator orchestrates, which failed now; None for any
    other run."""

    async def make() -> Opened | None:
        found = await system_traces.of_run(conn, run_id)
        lead = subject_lead(found)
        if found is None or lead is None:
            return None
        error = await rows.run_error(conn, run_id)
        line = "Error: " + runs.clip(" ".join(str(error or "its worker gave no reason").split()), 1200)
        key = f"{triage_rules.FAILED}:run:{run_id}"
        ask = Ask(found, lead, triage_rules.FAILED, [line], key=key, evidence=await _evidence(conn, run_id))
        return await open_thread(conn, ask)

    return await _safely(conn, triage_rules.FAILED, make)


async def on_draft(conn: AsyncConnection, run_id: int, lines: list[str], key: str) -> Opened | None:
    """The thread of rule draft (``draft_pulls``) about the draft pull request of the branch of run ``run_id``, whose
    agent an Orchestrator orchestrates, once for ``key``; None for an agent of no Orchestrator, or a key that has a
    thread."""

    async def make() -> Opened | None:
        found = await system_traces.of_run(conn, run_id)
        lead = subject_lead(found)
        if found is None or lead is None:
            return None
        ask = Ask(found, lead, triage_rules.DRAFT, lines, key=key, evidence=await _evidence(conn, run_id))
        return await open_thread(conn, ask)

    return await _safely(conn, triage_rules.DRAFT, make)


async def on_run(conn: AsyncConnection, found: RunStep, lines: list[str], *, stale: bool) -> Opened | None:
    """The thread of run ``found`` of ``triage.KINDS``, failed or ``stale``, of an agent an Orchestrator orchestrates,
    with ``lines`` (what the triage thread says of it) and its ``run_triage`` row, so the frame of ``triage_actions``
    holds in it; None for any other run, whose triage opens its thread as before."""
    from evo_agents.hub.server.triage import STALE_CAUSE  # triage calls this module

    rule = triage_rules.STALE if stale else triage_rules.FAILED

    async def make() -> Opened | None:
        member = await system_traces.of_run(conn, found.run_id)
        lead = subject_lead(member)
        if member is None or lead is None:
            return None
        key = f"{rule}:run:{found.run_id}"
        opened = await open_thread(
            conn, Ask(member, lead, rule, lines, key=key, evidence=await _evidence(conn, found.run_id))
        )
        if opened is not None:
            triage = {
                "run_id": found.run_id,
                "chat_id": opened.chat_id,
                "root_message_id": opened.root_id,
                "cause": STALE_CAUSE if stale else found.failure_cause,
                "cause_inferred": False,
                "agent_run_id": opened.run_id,
                "chain_depth": 0,
            }
            await rows.add_run_triage(conn, triage)
        return opened

    return await _safely(conn, rule, make)


# The rules of progress


async def _received(conn: AsyncConnection, trace_id: int, handle: str) -> list[tuple[int, str | None, str | None]]:
    """The work agent ``handle`` received in trace ``trace_id``, oldest first: (its span, the message, the goal of
    its brief); a run that goes on in a session is left out, its message the same, and a question asked of a session
    that took it in its inbox or went on with it counts by the span of its ask."""
    invoked, asks = await rows.received(conn, trace_id, handle, system_traces.RESUME)
    seen = {row.message_id for row in invoked if row.message_id is not None}
    asked = [(row.id, row.body, None) for row in asks if row.message_id not in seen]
    return sorted([(row[0], row[1], row[2]) for row in invoked] + asked)


async def check_loop(conn: AsyncConnection, run_id: int) -> Opened | None:
    """The loop thread of the trace of run ``run_id``, opened once its agent received the same work
    ``triage_rules.LOOP_COUNT`` times there; None otherwise."""

    async def make() -> Opened | None:
        found = await system_traces.of_run(conn, run_id)
        lead = subject_lead(found)
        if found is None or lead is None or found.trace_id is None:
            return None
        spans = triage_rules.loop_of(await _received(conn, found.trace_id, found.handle))
        if not spans:
            return None
        key = f"{triage_rules.LOOP}:trace:{found.trace_id}:@{found.handle}"
        line = f"It received the same work {len(spans)} times: the spans name each time."
        return await open_thread(conn, Ask(found, lead, triage_rules.LOOP, [line], key=key, evidence=tuple(spans)))

    return await _safely(conn, triage_rules.LOOP, make)


async def on_budget(conn: AsyncConnection, found: system_traces.Membership, span_id: int, reason: str) -> None:
    """The budget thread of the trace of ``found``, whose ask or mention span ``span_id`` was refused for ``reason``,
    the trace's budget; once a trace."""
    lead = subject_lead(found)
    if found is None or lead is None or found.trace_id is None:
        return
    key = f"{triage_rules.BUDGET}:trace:{found.trace_id}"
    ask = Ask(found, lead, triage_rules.BUDGET, [f"Refused: {reason}"], key=key, evidence=(span_id,))
    await _safely(conn, triage_rules.BUDGET, lambda: open_thread(conn, ask))


async def on_rounds(engine: AsyncEngine, run_id: int, rounds: int) -> Opened | None:
    """The thread of the turn of hub runtime run ``run_id`` that used its ``rounds`` without a final answer."""
    async with engine.begin() as conn:

        async def make() -> Opened | None:
            found = await system_traces.of_run(conn, run_id)
            lead = subject_lead(found)
            if found is None or lead is None:
                return None
            line = f"The turn used its {rounds} rounds of tools and gave no final answer."
            key = f"{triage_rules.ROUNDS}:run:{run_id}"
            evidence = await _evidence(conn, run_id)
            return await open_thread(conn, Ask(found, lead, triage_rules.ROUNDS, [line], key=key, evidence=evidence))

        return await _safely(conn, triage_rules.ROUNDS, make)


async def _idle_thread(conn: AsyncConnection, trace_id: int, quiet: timedelta) -> Opened | None:
    """The idle thread of trace ``trace_id``, quiet for ``quiet``, when that passes its limit and an agent of a
    running span of it has an Orchestrator."""
    spans = await rows.running_spans(conn, trace_id)
    runtime = "hub" if spans and all(span.runtime == "hub" for span in spans) else "worker"
    for span in spans:
        found = None if span.run_id is None else await system_traces.of_run(conn, span.run_id)
        lead = subject_lead(found)
        if found is None or lead is None:
            continue
        minutes = triage_rules.idle_minutes((found.spec or {}).get("limits"), runtime)
        if quiet < timedelta(minutes=minutes):
            return None
        line = f"No span for {int(quiet.total_seconds() // 60)} minutes, past its limit of {minutes} ({runtime} runs)."
        key = f"{triage_rules.IDLE}:trace:{trace_id}"
        evidence = tuple(item.id for item in spans)
        return await open_thread(conn, Ask(found, lead, triage_rules.IDLE, [line], key=key, evidence=evidence))
    return None


async def open_idle(engine: AsyncEngine, batch: int = IDLE_BATCH) -> int:
    """One pass, of the reaper (``run_reaper.recover_runs``): the idle thread of each idle trace, each in a transaction
    of its own; how many were opened."""
    async with engine.connect() as conn:
        found = await rows.idle_traces(conn, batch)
    opened = 0
    for row in found:
        async with engine.begin() as conn:
            make = partial(_idle_thread, conn, row.id, row.now - row.last_at)
            thread = await _safely(conn, triage_rules.IDLE, make)
            opened += thread is not None
    return opened


# Threads the work went past

RECOVER_BATCH = 50  # threads one pass of the reaper closes at most


async def close_recovered(engine: AsyncEngine, batch: int = RECOVER_BATCH) -> int:
    """One pass, of the reaper (``run_reaper.recover_runs``): each failed or stale thread whose agent's later run in
    the trace ended done closes, with no outcome (the Orchestrator decided nothing), a line of the hub in it, and the
    work card read again, so the trace stops showing failed once its work went on; each in a transaction of its own.
    A thread the hub could hand no Orchestrator run, or whose Orchestrator ended without an answer, stayed open
    forever before (trace 178). How many closed."""
    from evo_agents.hub.server import work_cards  # it reads threads through work_status, as this module writes them

    async with engine.connect() as conn:
        found = await rows.recovered_threads(conn, batch)
    closed = 0
    for row in found:
        try:
            async with engine.begin() as conn:
                if not await rows.close_open(conn, row.id):
                    continue
                if row.chat_id is not None and row.root_message_id is not None:
                    line = (
                        f"Closed: run #{row.run_id} of @{row.subject} in this trace ended done after run "
                        f"#{row.subject_run_id}, so the work went on."
                    )
                    await hub_reply(conn, row.chat_id, row.root_message_id, line)
                await work_cards.refresh(conn, row.trace_id)
                closed += 1
        except Exception as exc:  # one thread never stops the pass
            log.warning(
                "orchestrator thread not closed", extra={"thread": row.id, "error": f"{type(exc).__name__}: {exc}"}
            )
    return closed


# Escalation


async def _escalated(conn: AsyncConnection, run: AgentRun, row, found, lead: str, said: str, brief: dict):
    """(the thread, the wait, None) of the escalation of ``said``, or (None, None, why none), in a savepoint rolled
    back on a refusal."""
    from evo_agents.hub.server.agent_asks import open_wait  # it hands work through chat_runs, as this module does

    shown = system_context.brief_text(brief)
    lines = [f"@{found.handle} asks (run #{run.run_id}):", "", said.strip()]
    lines += ["", shown] if shown else []
    lines += ["", "Your answer, or the work of the agent you give it to, wakes the asking run."]
    nested = await conn.begin_nested()
    opened = await open_thread(
        conn, Ask(found, lead, triage_rules.ESCALATE, lines, evidence=_own_span(found), asker_run_id=run.run_id)
    )
    if opened is None or opened.run_id is None:
        await nested.rollback()
        return None, None, (opened.refusal if opened else None) or f"@{lead} is no agent the hub can hand work to"
    target = await by_handle(conn, lead)
    assert target is not None  # open_thread handed the work to this agent
    wait_id = await open_wait(conn, row, run, target, opened.run_id)
    await rows.set_wait(conn, opened.thread_id, wait_id)
    await system_traces.save_brief(conn, opened.run_id, run.run_id, brief)
    await nested.commit()
    return opened, wait_id, None


def _own_span(found: system_traces.Membership) -> tuple[int, ...]:
    return () if found.span_id is None else (found.span_id,)


async def escalate(conn: AsyncConnection, sealer, run: AgentRun, body: EscalateIn, row) -> Escalated:
    """Escalate ``body`` as the agent of chat run ``run`` (its locked ``row``, as ``held_plan_run`` reads it) to the
    Orchestrator of its system: a thread in the ops chat at the hop of the run, and a wait of the run that the
    Orchestrator's answer, or the end of the run it gives the work to, answers; the run parks on it and holds no
    worker. No hop is added and no budget refuses it. The worker's route and the hub runtime's tool share it."""
    chat = await chat_run(conn, run.run_id)
    assert chat is not None  # only a chat run escalates (hand_work)
    said = await mask_values(conn, sealer, run.profile.id, body.question)
    said = await mask_run(conn, sealer, run.run_id, said)
    found = await system_traces.of_run(conn, run.run_id)
    lead = None if found is None or found.node is None else system_context.escalation_target(found.spec, found.handle)
    opened = wait_id = None
    refusal = "this agent's system names no Orchestrator it escalates to" if lead is None else None
    if lead is not None:
        opened, wait_id, refusal = await _escalated(conn, run, row, found, lead, said, body.brief())
    if found is not None:
        await system_traces.record_tool(
            conn,
            found,
            system_context.TOOL_ESCALATE,
            to_handle=lead,
            status="ok" if refusal is None else "refused",
            reason=refusal,
            message_id=None if opened is None else opened.root_id,
            attributes={"evo.child_run_id": opened.run_id} if opened is not None else {},
        )
    if opened is not None:
        target = f"message:{opened.root_id} run:{run.run_id} escalated:{opened.run_id}"
        await Auditor(conn, None, run.principal).record(audit.CHAT_MESSAGE, target)
        where = "work" if opened.kind == chats.WORK else "ops"
        shown = (
            f"@{run.profile.handle} escalated to @{lead} (run #{opened.run_id}, {where} chat {opened.chat_id}): {said}"
        )
    else:
        shown = f"escalation: {refusal}"
    from evo_agents.hub.server.work_chats import (
        service as work_chats,  # it hands work through chat_runs, as this module does
    )

    await work_chats.say(conn, run.run_id, (chat.chat_id, chat.root_id), runs.clip(shown, 4000))
    log.info(
        "agent escalated", extra={"run_id": run.run_id, "made": opened and opened.run_id, "refused": bool(refusal)}
    )
    return Escalated(
        run_id=None if opened is None else opened.run_id,
        decision_id=wait_id,
        thread_id=None if opened is None else opened.thread_id,
        refusal=refusal,
    )


async def thread_at(conn: AsyncConnection, chat_id: int, root_id: int, *, lock: bool = False):
    """The row of the thread whose first message is ``root_id`` of chat ``chat_id``; None for none."""
    return await rows.thread_at(conn, chat_id, root_id, lock=lock)
