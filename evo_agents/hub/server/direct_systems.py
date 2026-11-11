"""Work a direct chat hands an agent system (``docs/hub.md``, Direct chats and Work chats). A person's message in a
direct chat, Home's included, that mentions agents the chat does not have goes, for each of them that is in an agent
system (``system_traces.system_for``), to the system's entry agent instead of to the agent: in the person's direct
chat with the entry agent (this chat when it is the entry agent's), as the person's message there, whose run opens a
trace (``system_traces.join``). That trace's work chat (``work_chats.open_work``) gets a copy of the message on its
main line, and the entry agent hands the work on from there; the agents named are not woken. The mention of each
keeps the entry agent's run, or why it made none.

An agent of no system, or of a system with no entry agent, still opens the group of the thread (``chat_groups``),
and a group a thread opened before is left as it is: only the agents of no system go to it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, tables
from evo_agents.hub.server import system_traces, work_chats
from evo_agents.hub.server.chat_access import ChatAccess, chat_access
from evo_agents.hub.server.chat_direct import direct_chat
from evo_agents.hub.server.chat_runs import Hand, hand_work
from evo_agents.hub.server.chat_store import insert_message
from evo_agents.hub.server.profile_store import Profile, by_id
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Message:
    """A person's message of a direct chat: the chat, its id and thread root, its text, and the credential kind the
    work it hands is dispatched with (runs.dispatched_via)."""

    chat: ChatAccess
    message_id: int
    root_id: int
    body: str
    via: str


@dataclass(frozen=True)
class Routed:
    """Where the work went: the system, its entry agent and run, and the work chat (None when the run is in no
    trace), with its title."""

    system: str
    entry: str
    run_id: int
    work_chat_id: int | None
    title: str

    def note(self, names: list[str]) -> str:
        """The hub's line in the message's thread: where the work of the agents ``names`` went."""
        who = ", ".join(f"@{name}" for name in names)
        if self.work_chat_id is None:
            return f"{who} of agent system {self.system}: the work went to its entry agent @{self.entry}."
        link = f"[{self.title}]({chats.chat_link(self.work_chat_id)})"
        return f"{who} of agent system {self.system}: the work went to its entry agent @{self.entry}, in {link}."


async def _system_of(conn: AsyncConnection, profile_id: int) -> tuple[system_traces.SystemRef, Profile] | None:
    """(the system agent ``profile_id`` is in, its entry agent); None for an agent of no system, or of a system with
    no entry agent that takes work."""
    profile = await by_id(conn, profile_id)
    if profile is None or profile.deleted_at is not None:
        return None
    held = await system_traces.system_for(conn, profile.project_id, profile.id, profile.handle, None)
    if held is None or held.id is None:
        return None
    s = tables.agent_systems
    entry_id = (await conn.execute(select(s.c.entry_profile_id).where(s.c.id == held.id))).scalar_one()
    entry = None if entry_id is None else await by_id(conn, entry_id)
    if entry is None or entry.deleted_at is not None:
        return None
    return held, entry


async def _place(
    conn: AsyncConnection, message: Message, user: Principal, entry: Profile
) -> tuple[ChatAccess, int, int]:
    """(the chat, message and thread root the entry agent takes the work in): the message itself when the direct chat
    is the entry agent's, else the person's words in their direct chat with it, outside a thread."""
    c = tables.chats
    query = select(c.c.direct_profile_id).where(c.c.id == message.chat.chat_id)
    if (await conn.execute(query)).scalar_one() == entry.id:
        return message.chat, message.message_id, message.root_id
    chat_id, _ = await direct_chat(conn, user, entry.handle)
    place = await chat_access(conn, user, chat_id)
    at, _ = await insert_message(conn, place, message.body, thread_root_id=None, author_user_id=user.user_id)
    return place, at, at


async def _to_entry(
    conn: AsyncConnection, message: Message, user: Principal, held: system_traces.SystemRef, entry: Profile
) -> Routed | str:
    """The work of ``message`` handed to ``entry``, and the work chat of the trace its run is in; why not, in a
    savepoint rolled back then."""
    nested = await conn.begin_nested()
    work, title = None, ""
    try:
        place, at, root = await _place(conn, message, user, entry)
        hand = Hand(place, at, root, entry, user.user_id, user.login, message.via, 1, user.login, message.body)
        run_id, why = await hand_work(conn, hand)
        trace = None if run_id is None else await work_chats.trace_of_run(conn, run_id)
        if trace is not None and trace.system_id is not None:
            work = await work_chats.open_work(conn, trace, user)
            board = await chat_access(conn, user, work)
            await insert_message(
                conn, board, message.body, thread_root_id=None, author_user_id=user.user_id, copied_from_id=at
            )
            c = tables.chats
            title = (await conn.execute(select(c.c.title).where(c.c.id == work))).scalar_one()
    except HTTPException as exc:
        run_id, why = None, str(exc.detail)
    if run_id is None:
        await nested.rollback()
        return f"its entry agent @{entry.handle} took no work: {why}"
    await nested.commit()
    log.info("direct mention to a system", extra={"chat_id": message.chat.chat_id, "run_id": run_id, "work": work})
    return Routed(held.slug, entry.handle, run_id, work, title)


async def to_systems(conn: AsyncConnection, message: Message, user: Principal, kept: list[dict]) -> list[str]:
    """Hand the work of the mentions ``kept`` of a direct chat's ``message`` that name agents of a system, not in the
    chat, to that system's entry agent (see the module's docstring); the hub's lines for the thread. Each such mention
    gets ``run_id``, the entry agent's run, or None and its refusal; the others are left as they are."""
    if message.chat.kind != chats.DIRECT:
        return []
    systems: dict[int, tuple[system_traces.SystemRef, Profile, list[dict]]] = {}
    for mention in kept:
        if mention["profile_id"] is None or mention["via"] is not None:
            continue
        found = await _system_of(conn, mention["profile_id"])
        if found is not None:
            held, entry = found
            systems.setdefault(entry.id, (held, entry, []))[2].append(mention)
    said = []
    for held, entry, mentions in systems.values():
        routed = await _to_entry(conn, message, user, held, entry)
        taken = routed if isinstance(routed, Routed) else None
        for mention in mentions:
            mention["run_id"] = None if taken is None else taken.run_id
            mention["refusal"] = None if taken is not None else f"@{mention['name']}: {routed}"
        if taken is not None:
            said.append(taken.note([mention["name"] for mention in mentions]))
    return said
