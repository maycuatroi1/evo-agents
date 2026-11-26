"""Group chats a direct chat opens (``docs/hub.md``, Direct chats). A direct chat holds one person and one agent for
good; a mention there of an agent it does not have, by its person (``chats.service.messages.deliver_message``) or by its
agent (``evo-agents worker chat mention``, ``chats.service.agents.mention``), opens a group chat of that person, the
direct chat's agent and the agents mentioned, and the work goes on there. The group keeps the first message of the
direct chat's thread it came from (``chats.opened_from_message_id``, unique), so a later mention in that thread adds to
the same group instead of opening another.

Each agent goes in with the checks of adding it to a chat (``chats.service.members.addable_agent`` for the person, then
``put_agent``: writer on its project, not deleted, a name no person has, the people clearing its project's label); one
that fails is left out with why, and when none goes in no group is made. The person adds them: the members' events
name them, or the direct chat's agent when its run asked. Opening one is audited as chat.create in the direct agent's
project, naming the direct chat and the thread.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats
from evo_agents.hub.server import audit
from evo_agents.hub.server.chats.models.access import ChatAccess
from evo_agents.hub.server.chats.models.groups import CONTROL, TOPIC_CHARS, Opened
from evo_agents.hub.server.chats.models.views import person_author
from evo_agents.hub.server.chats.repository import groups as store
from evo_agents.hub.server.chats.repository.store import add_member, member_event
from evo_agents.hub.server.chats.service.access import chat_access
from evo_agents.hub.server.chats.service.members import addable_agent, put_agent
from evo_agents.hub.server.chats.service.notices import notice_added
from evo_agents.hub.server.profile_store import Profile, by_id
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)


def _title(lead: Profile, first: str | None, handles: list[str]) -> str:
    line = next((line.strip() for line in (first or "").splitlines() if line.strip()), "")
    topic = " ".join(CONTROL.sub(" ", line).split())
    if len(topic) > TOPIC_CHARS:
        topic = topic[: TOPIC_CHARS - 1].rstrip() + "…"
    head = f"{lead.name} + " + ", ".join(f"@{handle}" for handle in handles)
    title = f"{head}: {topic}" if topic else head
    return title if len(title) <= chats.MAX_TITLE_CHARS else title[: chats.MAX_TITLE_CHARS - 1].rstrip() + "…"


async def _make(
    conn: AsyncConnection, person: Principal, lead: Profile, root: int | None, title: str, run_id: int | None
) -> int | None:
    """The group of ``person`` and ``lead`` for the thread of ``root``, made now; None when another request made it
    first (``store.found`` finds it once that transaction commits)."""
    chat_id = await store.insert_group(conn, person.user_id, root, title)
    if chat_id is None:
        return None
    by = f"@{lead.handle}" if run_id is not None else person.login
    await add_member(conn, chat_id, user_id=person.user_id, added_by=person.user_id, added_by_run_id=run_id)
    await member_event(conn, chat_id, "member_added", person_author(person.login), by)
    return chat_id


async def _put(conn: AsyncConnection, person: Principal, chat: ChatAccess, profile: Profile) -> str | None:
    """Add ``profile`` to the group, in a savepoint: None once it is in, else why not (nothing written)."""
    savepoint = await conn.begin_nested()
    try:
        await put_agent(conn, person, chat, profile)
    except HTTPException as exc:
        await savepoint.rollback()
        return str(exc.detail)
    await savepoint.commit()
    return None


async def open_from(
    conn: AsyncConnection,
    direct: ChatAccess,
    person: Principal,
    root: int | None,
    handles: list[str],
    *,
    run_id: int | None = None,
) -> Opened:
    """The group of direct chat ``direct``'s thread ``root`` (the one it opened before, or one made now) with
    ``person``, the direct chat's agent and the agents ``handles`` names that may go in; ``run_id`` names the run of
    the direct chat's agent that asked, None for the person's own mention. See the module's docstring."""
    lead = await by_id(conn, await store.direct_profile_id(conn, direct.chat_id))
    assert lead is not None  # a direct chat's agent stays, deleted or not
    opened = Opened(lead=lead.handle)
    wanted = []
    for handle in dict.fromkeys(handles):
        try:
            wanted.append(await addable_agent(conn, person, handle))
        except HTTPException as exc:
            opened.refused[handle] = str(exc.detail)
    if not wanted:
        return opened
    savepoint = await conn.begin_nested()
    found = await store.found(conn, root)
    if found is None:
        first = None if root is None else await store.message_body(conn, root)
        title = _title(lead, first, [profile.handle for profile in wanted])
        made = await _make(conn, person, lead, root, title, run_id)
        found = (made, title) if made is not None else await store.found(conn, root)
        opened.made = made is not None
    assert found is not None  # made now, or by the request that made it first
    opened.chat_id, opened.title = found
    chat = await chat_access(conn, person, opened.chat_id)
    if opened.made and lead.deleted_at is None:
        await _put(conn, person, chat, lead)
    for profile in wanted:
        why = await _put(conn, person, chat, profile)
        if why is None:
            opened.members.append(profile.handle)
        else:
            opened.refused[profile.handle] = why
    if not opened.members:
        await savepoint.rollback()
        return Opened(lead=lead.handle, refused=opened.refused)
    await savepoint.commit()
    if opened.made:
        target = f"chat:{opened.chat_id} opened from chat:{direct.chat_id} message:{root}"
        actor = {"actor_id": person.user_id, "token_id": person.token_id}
        await audit.record(conn, **actor, action=audit.CHAT_CREATE, target=target, project_id=lead.project_id)
        if run_id is not None:  # the agent opened it: the person hears of it as of any chat an agent makes
            await notice_added(conn, await _as_agent(conn, opened.chat_id, lead, person), person.user_id)
    log.info(
        "group chat from a direct chat",
        extra={"chat_id": opened.chat_id, "direct": direct.chat_id, "made": opened.made, "run_id": run_id},
    )
    return opened


async def _as_agent(conn: AsyncConnection, chat_id: int, lead: Profile, person: Principal) -> ChatAccess:
    from evo_agents.hub.server.chats.service.runs import agent_access  # it imports the messages, which import this

    found = await agent_access(conn, chat_id, lead, person.user_id)
    assert found is not None  # the lead went in with the group
    return found
