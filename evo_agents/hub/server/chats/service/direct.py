"""Direct chats (``chats.DIRECT``): PUT /v1/chats/direct/{handle} opens the caller's chat with one agent, the one
there is (200) or one made now (201). ``docs/hub.md`` (Group chats, Direct chats) is the design.

A person has one direct chat with each agent, held by the key chats_direct_key of (person, agent): two requests at once
make one chat, the second inserting nothing and finding the first's. Making one takes the checks of adding the agent to
a chat (``chats.service.members.addable_agent``: 404 for an agent the caller cannot see, 403 without the writer role on
its project, 409 for a deleted one), a person and an agent of different names, and the caller clearing the label of the
agent's project (409 otherwise). A direct chat there is opens without them, so the one of an agent deleted since still
reads; ``chats.service.access`` decides who reads it, as for any chat. Its title is the agent's name when it was made;
it is audited as chat.create in the agent's project, naming the chat and the agent.

Its members never change (``chats.service.members.refuse_direct``). A message of its person that mentions no agent of
the chat is work for its agent, as if it mentioned it (``direct_mention``, from ``chats.service.messages``).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.server import audit
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.chats.models.access import ChatAccess
from evo_agents.hub.server.chats.models.views import Chat, agent_author, person_author
from evo_agents.hub.server.chats.repository import direct as store
from evo_agents.hub.server.chats.repository.access import projects_by_id
from evo_agents.hub.server.chats.repository.store import add_member, member_event
from evo_agents.hub.server.chats.service.access import chat_access, uncleared
from evo_agents.hub.server.chats.service.members import addable_agent, refuse_clash, refuse_uncleared
from evo_agents.hub.server.chats.service.views import chat_view
from evo_agents.hub.server.profile_store import Profile, by_handle
from evo_agents.hub.server.security import Principal


async def _make(conn: AsyncConnection, user: Principal, profile: Profile) -> int | None:
    """Make the direct chat of ``user`` and agent ``profile``, with both in it, its events and its audit row; its id,
    or None when another request made it first (its transaction committed, so ``store.found`` finds it)."""
    chat_id = await store.insert_direct(conn, user.user_id, profile)
    if chat_id is None:
        return None
    await add_member(conn, chat_id, user_id=user.user_id, added_by=user.user_id)
    await member_event(conn, chat_id, "member_added", person_author(user.login), user.login)
    await add_member(conn, chat_id, profile_id=profile.id, added_by=user.user_id)
    await member_event(conn, chat_id, "member_added", agent_author(profile), user.login)
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.CHAT_CREATE,
        target=f"chat:{chat_id} direct agent:@{profile.handle}",
        project_id=profile.project_id,
    )
    return chat_id


async def direct_chat(conn: AsyncConnection, user: Principal, handle: str) -> tuple[int, bool]:
    """(the id of ``user``'s direct chat with agent ``handle``, whether it was made now), in the caller's transaction;
    the refusals of the module's docstring when there is none and it cannot be made."""
    profile = await by_handle(conn, handle)
    found = None if profile is None else await store.found(conn, user.user_id, profile.id)
    if found is not None:
        return found, False
    profile = await addable_agent(conn, user, handle)
    refuse_clash([user.login], [profile.handle])
    projects = await projects_by_id(conn, [profile.project_id])
    refuse_uncleared(await uncleared(conn, {user.user_id: user.login}, projects), f"a direct chat with @{handle}")
    made = await _make(conn, user, profile)
    if made is not None:
        return made, True
    return (await store.found(conn, user.user_id, profile.id)), False


async def open_direct(engine: AsyncEngine, user: Principal, handle: str) -> tuple[Chat, int, bool]:
    """``user``'s direct chat with agent ``handle`` (``direct_chat``), in a transaction of its own: (the chat as
    ``user`` reads it, its id, whether it was made now)."""
    async with UnitOfWork(engine) as conn:
        chat_id, made = await direct_chat(conn, user, handle)
        return await chat_view(conn, await chat_access(conn, user, chat_id)), chat_id, made


async def direct_mention(conn: AsyncConnection, chat: ChatAccess, kept: list[dict]) -> list[dict]:
    """The mention of the agent of ``chat``, a direct or a settle chat (``chats.ATTENDED``), ``via`` the chat's kind,
    for a message whose mentions ``kept`` do not reach it; none when they do, so that a message makes one run of it at
    most."""
    row = await store.direct_agent(conn, chat.chat_id)
    if any(mention["profile_id"] == row.id for mention in kept):
        return []
    return [{"name": row.handle, "via": chat.kind, "user_id": None, "profile_id": row.id, "refusal": None}]
