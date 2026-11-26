"""Group chats a direct chat opens (``docs/hub.md``, Direct chats). A direct chat holds one person and one agent for
good; a mention there of an agent it does not have, by its person (``chats.deliver_message``) or by its agent
(``evo-agents worker chat mention``, ``chat_agents.mention``), opens a group chat of that person, the direct chat's
agent and the agents mentioned, and the work goes on there. The group keeps the first message of the direct chat's
thread it came from (``chats.opened_from_message_id``, unique), so a later mention in that thread adds to the same group
instead of opening another.

Each agent goes in with the checks of adding it to a chat (``chat_members.addable_agent`` for the person, then
``put_agent``: writer on its project, not deleted, a name no person has, the people clearing its project's label); one
that fails is left out with why, and when none goes in no group is made. The person adds them: the members' events
name them, or the direct chat's agent when its run asked. Opening one is audited as chat.create in the direct agent's
project, naming the direct chat and the thread.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, tables
from evo_agents.hub.server import audit
from evo_agents.hub.server.chat_access import ChatAccess, chat_access
from evo_agents.hub.server.chat_members import addable_agent, put_agent
from evo_agents.hub.server.chat_notices import notice_added
from evo_agents.hub.server.chat_store import add_member, member_event
from evo_agents.hub.server.chat_views import person_author
from evo_agents.hub.server.profile_store import Profile, by_id
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)

CONTROL = re.compile(r"[\x00-\x1f\x7f]")
TOPIC_CHARS = 60  # of the thread's first line in the group's title


@dataclass
class Opened:
    """The group a direct chat's mention went to: its id and title (None when it took none of the agents), whether
    it was made now, the agents asked that are in it, and why each of the others is not."""

    chat_id: int | None = None
    title: str = ""
    made: bool = False
    lead: str = ""
    members: list[str] = field(default_factory=list)
    refused: dict[str, str] = field(default_factory=dict)

    def note(self) -> str:
        """The hub's message in the direct chat's thread: where the work went."""
        link = f"[{self.title}]({chats.chat_link(self.chat_id or 0)})"  # a note names a group that took an agent
        who = ", ".join(f"@{handle}" for handle in self.members)
        if self.made:
            return f"Opened the group chat {link} of you, @{self.lead} and {who}: the work goes on there."
        return f"The work goes on in the group chat {link} of this thread, with {who}."


def _title(lead: Profile, first: str | None, handles: list[str]) -> str:
    line = next((line.strip() for line in (first or "").splitlines() if line.strip()), "")
    topic = " ".join(CONTROL.sub(" ", line).split())
    if len(topic) > TOPIC_CHARS:
        topic = topic[: TOPIC_CHARS - 1].rstrip() + "…"
    head = f"{lead.name} + " + ", ".join(f"@{handle}" for handle in handles)
    title = f"{head}: {topic}" if topic else head
    return title if len(title) <= chats.MAX_TITLE_CHARS else title[: chats.MAX_TITLE_CHARS - 1].rstrip() + "…"


async def _found(conn: AsyncConnection, root: int | None) -> tuple[int, str] | None:
    if root is None:
        return None
    c = tables.chats
    row = (await conn.execute(select(c.c.id, c.c.title).where(c.c.opened_from_message_id == root))).one_or_none()
    return None if row is None else (row.id, row.title)


async def _make(
    conn: AsyncConnection, person: Principal, lead: Profile, root: int | None, title: str, run_id: int | None
) -> int | None:
    """The group of ``person`` and ``lead`` for the thread of ``root``, made now; None when another request made it
    first (``_found`` finds it once that transaction commits)."""
    c = tables.chats
    statement = pg_insert(c).values(
        title=title, created_by=person.user_id, kind=chats.GROUP, opened_from_message_id=root
    )
    if root is not None:
        statement = statement.on_conflict_do_nothing(
            index_elements=[c.c.opened_from_message_id], index_where=c.c.opened_from_message_id.is_not(None)
        )
    chat_id = (await conn.execute(statement.returning(c.c.id))).scalar_one_or_none()
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
    c = tables.chats
    lead_id = (await conn.execute(select(c.c.direct_profile_id).where(c.c.id == direct.chat_id))).scalar_one()
    lead = await by_id(conn, lead_id)
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
    found = await _found(conn, root)
    if found is None:
        m = tables.chat_messages
        first = None if root is None else (await conn.execute(select(m.c.body).where(m.c.id == root))).scalar_one()
        title = _title(lead, first, [profile.handle for profile in wanted])
        made = await _make(conn, person, lead, root, title, run_id)
        found = (made, title) if made is not None else await _found(conn, root)
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
    from evo_agents.hub.server.chat_runs import agent_access  # it imports the chats routes, which import this module

    found = await agent_access(conn, chat_id, lead, person.user_id)
    assert found is not None  # the lead went in with the group
    return found


async def has_agent(conn: AsyncConnection, chat_id: int, handle: str) -> bool:
    """Whether agent ``handle`` is in chat ``chat_id`` now."""
    m, ap = tables.chat_members, tables.agent_profiles
    query = (
        select(m.c.id)
        .join_from(m, ap, ap.c.id == m.c.profile_id)
        .where(m.c.chat_id == chat_id, ap.c.handle == handle, m.c.removed_at.is_(None))
    )
    return (await conn.execute(query)).first() is not None
