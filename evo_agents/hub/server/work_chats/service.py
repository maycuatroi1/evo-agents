"""The work chat of a trace of an agent system (``chats.WORK``; ``docs/hub.md``, Work chats): the one group chat where
the agents of the system that the trace's work goes to answer, each in a thread of its own.

The first hand-off of a trace opens it (``open_work``): an agent ask (``agent_asks``), an escalation or any other
thread of the Orchestrator about a run of the trace (``orchestrator``), or the work the Orchestrator gives
(``orchestrator_actions``). ``system_traces.work_chat_id`` names it; an advisory lock per trace, under which that
column is read again, has concurrent hand-offs open one chat. Its creator is the person who opened the trace, with no
notice; the coordinator (the system's entry agent) is put in it, and each agent the work goes to, as the hub's own
flows put agents (``chats.service.members.put_agent``, sharing nothing). A trace with no hand-off has none.

A hand-off (``hand_over``) puts the question as a message of the asking agent in the work chat: in the thread of the
agent asked when it has one there, so its session, worktree and branch go on, else on the main line, where it opens
that thread. An agent of another project than the system's, or one the chat cannot take (``chats.MAX_AGENTS``, or a
label a member does not clear), gets the work in the direct chat of the person at the origin, as an ask outside a
system does, and a line of the hub in the work chat names the agent, its project and the run. The coordinator asked
answers in its one session, the thread of the trace's first message: the question goes on the main line and a copy of
it into that thread.

What the coordinator writes in that thread is copied onto the main line (``copy_out``), and a person's message on the
main line that mentions no agent goes to the coordinator's session as a copy in that thread (``to_coordinator``); a
copy names the message it copies (``chat_messages.copied_from_id``) and hands no work. The lines of the hub about a
hand-off (asked, answered, refused, gave no answer, withheld, escalated) go to the work chat (``line_place``). A line
that repeats a message of the work chat (the question an "asked" line shows, the answer an "answered" line shows) names
it (``chat_messages.repeats_id``, the ``repeats`` of the chat API), so a page may fold it; a line in any other chat,
and every line before revision 0270, names none.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import cast

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, runs
from evo_agents.hub.server import audit, system_traces
from evo_agents.hub.server.chats.models.access import ChatAccess
from evo_agents.hub.server.chats.models.runs import Hand
from evo_agents.hub.server.chats.models.views import person_author
from evo_agents.hub.server.chats.repository.store import add_member, member_event
from evo_agents.hub.server.chats.service.access import chat_access
from evo_agents.hub.server.chats.service.direct import direct_chat
from evo_agents.hub.server.chats.service.members import label_now, put_agent, put_person
from evo_agents.hub.server.chats.service.posting import insert_message
from evo_agents.hub.server.chats.service.runs import agent_access, hand_work, hub_reply
from evo_agents.hub.server.profile_store import Profile, by_id
from evo_agents.hub.server.security import MACHINE, Principal
from evo_agents.hub.server.work_chats import repository
from evo_agents.hub.server.work_chats.models import STEP_THREADS_SCAN, TITLE, TITLE_WORDS, Handed, Trace
from evo_agents.hub.server.work_chats.repository import trace_of_chat, trace_of_run

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Asker:
    """Who hands work over: the agent and run that write the question, the person at the origin, the credential
    kind and the hop of the work, and the priority of the run it asks for (runs.PRIORITIES)."""

    profile: Profile
    run_id: int
    origin: Principal
    via: str
    hop: int
    priority: str = runs.DEFAULT_PRIORITY

    @property
    def author(self) -> str:
        return f"@{self.profile.handle}"

    def hand(self, chat: ChatAccess, message_id: int, root_id: int, target: Profile, text: str) -> Hand:
        """The work of ``target`` it asks for with message ``message_id`` in the thread of ``root_id`` of ``chat``."""
        origin = self.origin
        hand = Hand(
            chat, message_id, root_id, target, origin.user_id, origin.login, self.via, self.hop, self.author, text
        )
        return replace(hand, priority=self.priority)


async def _person(conn: AsyncConnection, user_id: int) -> Principal:
    login = await repository.login_of(conn, user_id)
    return Principal(user_id, login, False, cast(int, None), MACHINE, "")


# Opening the work chat


async def _title(conn: AsyncConnection, trace: Trace) -> str:
    body = None
    if trace.message_id is not None:
        body = await repository.body_of(conn, trace.message_id)
    words = " ".join((body or "").split())
    what = (words[:TITLE_WORDS] + "...") if len(words) > TITLE_WORDS else words or f"trace #{trace.id}"
    return TITLE.format(what=what)[: chats.MAX_TITLE_CHARS]


async def _made(conn: AsyncConnection, trace: Trace, creator: Principal) -> int:
    values = {"title": await _title(conn, trace), "created_by": creator.user_id, "kind": chats.WORK}
    chat_id = await repository.add_chat(conn, values)
    await add_member(conn, chat_id, user_id=creator.user_id, added_by=creator.user_id)
    await member_event(conn, chat_id, "member_added", person_author(creator.login), creator.login)
    await audit.record(
        conn,
        actor_id=creator.user_id,
        token_id=None,
        action=audit.CHAT_CREATE,
        target=f"chat:{chat_id} trace:{trace.id}",
        project_id=trace.project_id,
    )
    return chat_id


async def open_work(conn: AsyncConnection, trace: Trace, person: Principal) -> int:
    """The work chat of ``trace``: made at its first hand-off, under the trace's advisory lock, its creator the person
    who opened the trace (else ``person``), with the coordinator in it and the trace's work card posted in its root thread
    (``work_cards``); its id."""
    await repository.lock_trace(conn, trace.id)
    found = await repository.work_chat_of(conn, trace.id)
    if found is not None:
        return found
    creator = person if trace.opened_by is None else await _person(conn, trace.opened_by)
    chat_id = await _made(conn, trace, creator)
    await repository.set_work_chat(conn, trace.id, chat_id)
    coordinator = None if trace.coordinator_id is None else await by_id(conn, trace.coordinator_id)
    if coordinator is not None and coordinator.deleted_at is None:
        _, why = await put_in(conn, chat_id, coordinator)
        if why is not None:
            log.warning("coordinator not in the work chat", extra={"trace_id": trace.id, "why": why})
    from evo_agents.hub.server.work_cards import post_card  # it reads the traces through this module

    await post_card(conn, trace)
    log.info("work chat opened", extra={"trace_id": trace.id, "chat_id": chat_id})
    return chat_id


async def put_in(conn: AsyncConnection, chat_id: int, profile: Profile) -> tuple[ChatAccess | None, str | None]:
    """(work chat ``chat_id`` as its creator sees it, None) once agent ``profile`` is in it, or (None, why it may not
    be), in a savepoint."""
    from evo_agents.hub.server.triage.service.chats import (
        as_member,  # triage hands work through chat_runs, as this module does
    )

    nested = await conn.begin_nested()
    try:
        chat, member = await as_member(conn, chat_id)
        if await put_agent(conn, member, chat, profile):
            chat = await label_now(conn, chat)
    except HTTPException as exc:
        await nested.rollback()
        return None, str(exc.detail)
    await nested.commit()
    return chat, None


async def _with_person(conn: AsyncConnection, chat: ChatAccess, person: Principal) -> str | None:
    """Why ``person``, at the origin of a hand-off, may not be in work chat ``chat`` (seen by its creator); None once
    they are."""
    if person.user_id == chat.user_id:
        return None
    nested = await conn.begin_nested()
    try:
        await put_person(conn, _as_principal(chat), chat, person.user_id, person.login)
    except HTTPException as exc:
        await nested.rollback()
        return str(exc.detail)
    await nested.commit()
    return None


def _as_principal(chat: ChatAccess) -> Principal:
    """The person ``chat`` is seen by, who adds the others (``triage.as_member``)."""
    return Principal(chat.user_id, chat.login, False, cast(int, None), MACHINE, "")


async def placed(
    conn: AsyncConnection, trace: Trace, origin: Principal, target: Profile
) -> tuple[int, ChatAccess | None, str | None]:
    """(the work chat of ``trace``, it as its creator sees it with ``target`` in it, None), or (the work chat, None,
    why ``target`` takes its work elsewhere)."""
    work = await open_work(conn, trace, origin)
    if target.project_id != trace.project_id:
        return work, None, f"@{target.handle} is of project {target.project}, not of the system's"
    chat, why = await put_in(conn, work, target)
    if chat is not None:
        why = await _with_person(conn, chat, origin)
    return work, (chat if why is None else None), why


# Handing work over


async def _direct(conn: AsyncConnection, asker: Asker, target: Profile, text: str) -> Handed:
    """The question in the direct chat of the origin with ``target``, as an ask outside a system is."""
    chat_id, _ = await direct_chat(conn, asker.origin, target.handle)
    chat = await chat_access(conn, asker.origin, chat_id)
    message_id, _ = await insert_message(
        conn, chat, text, thread_root_id=None, author_profile_id=asker.profile.id, run_id=asker.run_id
    )
    hand = asker.hand(chat, message_id, message_id, target, text)
    child, why = await hand_work(conn, hand)
    return Handed(child, message_id, why, (chat_id, message_id))


async def agent_thread(
    conn: AsyncConnection, chat_id: int, profile_id: int, steps: frozenset[tuple[str, str]] = frozenset()
) -> int | None:
    """The thread of agent ``profile_id`` in work chat ``chat_id``: that of its latest run there, a thread of the
    Orchestrator left out; for an ask that names plan ``steps`` (``system_traces.ref_steps``), that of its latest run
    whose brief names one of them, so each step has a session of its own; None when it has none."""
    if not steps:
        return await repository.latest_thread(conn, chat_id, profile_id)
    for found, brief in await repository.threads(conn, chat_id, profile_id, STEP_THREADS_SCAN):
        if steps & system_traces.ref_steps(brief):
            return found
    return None


async def _in_work(
    conn: AsyncConnection,
    chat: ChatAccess,
    asker: Asker,
    target: Profile,
    text: str,
    steps: frozenset[tuple[str, str]] = frozenset(),
) -> Handed:
    """The question in the thread of ``target`` in work chat ``chat`` (of ``steps``, when the ask names plan steps),
    or on its main line, where it opens one."""
    root = await agent_thread(conn, chat.chat_id, target.id, steps)
    message_id, _ = await insert_message(
        conn, chat, text, thread_root_id=root, author_profile_id=asker.profile.id, run_id=asker.run_id
    )
    root = root or message_id
    hand = asker.hand(chat, message_id, root, target, text)
    child, why = await hand_work(conn, hand)
    return Handed(child, message_id, why, (chat.chat_id, root))


async def _to_root(conn: AsyncConnection, chat: ChatAccess, trace: Trace, asker: Asker, target: Profile, text: str):
    """The question to the coordinator: on the main line of work chat ``chat``, and its copy in the coordinator's
    session, the thread of the trace's first message, which takes the work."""
    from evo_agents.hub.server.triage.service.chats import (
        as_member,  # triage hands work through chat_runs, as this module does
    )

    assert trace.chat_id is not None and trace.root_id is not None  # the caller asks the coordinator there only
    main_id, _ = await insert_message(
        conn, chat, text, thread_root_id=None, author_profile_id=asker.profile.id, run_id=asker.run_id
    )
    root, _ = await as_member(conn, trace.chat_id)
    copy_id, _ = await insert_message(
        conn,
        root,
        text,
        thread_root_id=trace.root_id,
        author_profile_id=asker.profile.id,
        run_id=asker.run_id,
        copied_from_id=main_id,
    )
    hand = asker.hand(root, copy_id, trace.root_id, target, text)
    child, why = await hand_work(conn, hand)
    return Handed(child, main_id, why, (chat.chat_id, main_id))


async def _hand_over(
    conn: AsyncConnection, trace: Trace | None, asker: Asker, target: Profile, text: str, steps: frozenset
) -> Handed:
    if trace is None or trace.system_id is None:
        return await _direct(conn, asker, target, text)
    work, chat, why = await placed(conn, trace, asker.origin, target)
    if chat is None:
        handed = await _direct(conn, asker, target, text)
        if handed.child is not None:
            line = (
                f"@{target.handle} of project {target.project} takes this work in the direct chat of "
                f"{asker.origin.login} (run #{handed.child}), not in this chat: {why}"
            )
            await hub_reply(conn, work, None, line)
        return handed
    if target.id == trace.coordinator_id and trace.chat_id is not None and trace.root_id is not None:
        return await _to_root(conn, chat, trace, asker, target, text)
    return await _in_work(conn, chat, asker, target, text, steps)


async def hand_over(
    conn: AsyncConnection, asker: Asker, target: Profile, text: str, steps: frozenset[tuple[str, str]] = frozenset()
) -> Handed:
    """Hand ``target`` the work ``text`` asks, written by ``asker``, of plan ``steps`` when it names some: in the work
    chat of the asking run's trace of an agent system (see the module's docstring), else in the origin's direct chat
    with ``target``; in a savepoint rolled back on a refusal, the opening of the work chat with it."""
    trace = await trace_of_run(conn, asker.run_id)
    nested = await conn.begin_nested()
    try:
        handed = await _hand_over(conn, trace, asker, target, text, steps)
    except HTTPException as exc:
        await nested.rollback()
        return Handed(None, None, str(exc.detail))
    if handed.child is None:
        await nested.rollback()
        return Handed(None, None, handed.why)
    await nested.commit()
    return handed


async def line_place(
    conn: AsyncConnection, run_id: int, own: tuple[int, int], there: tuple[int, int] | None = None
) -> tuple[int, int | None]:
    """Where a line of the hub about a hand-off of run ``run_id`` goes: in the work chat of its trace, in the thread
    ``there`` of the agent asked when that is in it, else in the run's own thread ``own`` when that is, else on its
    main line; ``own`` for a run whose trace has no work chat."""
    trace = await trace_of_run(conn, run_id)
    work = None if trace is None else trace.work_chat_id
    if work is None:
        return own
    if there is not None and there[0] == work:
        return there
    if own[0] == work:
        return own
    return work, None


async def say(
    conn: AsyncConnection,
    run_id: int,
    own: tuple[int, int],
    text: str,
    there: tuple[int, int] | None = None,
    *,
    repeats: int | None = None,
) -> int:
    """The line ``text`` of the hub about a hand-off of run ``run_id``, where ``line_place`` puts it; its id. It names
    message ``repeats``, which it repeats, when the line is in a work chat and that message is of the same chat."""
    chat_id, root_id = await line_place(conn, run_id, own, there)
    message_id = await hub_reply(conn, chat_id, root_id, text)
    if repeats is not None:
        await repository.mark_repeats(conn, message_id, chat_id, repeats)
    return message_id


# Copies between the work chat and the coordinator's session


async def copy_out(
    conn: AsyncConnection, chat: ChatAccess, profile: Profile, run_id: int, message_id: int, root_id: int, body: str
) -> int | None:
    """The copy, on the main line of the trace's work chat, of message ``message_id`` that the coordinator's run
    ``run_id`` wrote in the thread of ``root_id`` of ``chat``, when that is its session, the thread of the trace's
    first message, and every project of ``chat`` is of the work chat too; its id, None for any other message."""
    if chat.kind == chats.WORK:
        return None
    trace = await trace_of_run(conn, run_id)
    if (
        trace is None
        or trace.work_chat_id is None
        or profile.id != trace.coordinator_id
        or (chat.chat_id, root_id) != (trace.chat_id, trace.root_id)
    ):
        return None
    work = await agent_access(conn, trace.work_chat_id, profile, chat.user_id)
    if work is None or not {p.name for p in chat.projects} <= {p.name for p in work.projects}:
        return None
    copy_id, _ = await insert_message(
        conn, work, body, thread_root_id=None, author_profile_id=profile.id, copied_from_id=message_id
    )
    return copy_id


async def main_line(conn: AsyncConnection, root: int | None) -> bool:
    """Whether a message in the thread of ``root`` of a work chat is on its main line: outside a thread, or in the
    thread of a copy of the coordinator's words."""
    if root is None:
        return True
    return await repository.copied_from(conn, root) is not None


async def to_coordinator(
    conn: AsyncConnection, chat: ChatAccess, user: Principal, message_id: int, body: str, via: str
) -> str | None:
    """Hand message ``message_id`` of ``user`` on the main line of work chat ``chat``, which mentions no agent, to the
    coordinator's session: a copy in the thread of the trace's first message; the hub's answer when it goes nowhere,
    None when it went."""
    trace = await trace_of_chat(conn, chat.chat_id)
    if trace is None or trace.coordinator_id is None or trace.chat_id is None or trace.root_id is None:
        return None
    coordinator = await by_id(conn, trace.coordinator_id)
    if coordinator is None or coordinator.deleted_at is not None:
        return None
    if user.user_id != trace.opened_by:
        who = (
            "the person who opened the work"
            if trace.opened_by is None
            else (await _person(conn, trace.opened_by)).login
        )
        return f"a message with no @ goes to @{coordinator.handle} for {who} alone: mention the agent it is for"
    nested = await conn.begin_nested()
    try:
        root = await chat_access(conn, user, trace.chat_id)
        copy_id, _ = await insert_message(
            conn, root, body, thread_root_id=trace.root_id, author_user_id=user.user_id, copied_from_id=message_id
        )
        hand = Hand(root, copy_id, trace.root_id, coordinator, user.user_id, user.login, via, 1, user.login, body)
        child, why = await hand_work(conn, hand)
    except HTTPException as exc:
        child, why = None, str(exc.detail)
    if child is None:
        await nested.rollback()
        return f"@{coordinator.handle}: {why}"
    await nested.commit()
    return None
