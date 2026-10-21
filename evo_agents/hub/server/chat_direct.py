"""Direct chats (``chats.DIRECT``): PUT /v1/chats/direct/{handle} opens the caller's chat with one agent, the one
there is (200) or one made now (201). ``docs/hub.md`` (Group chats, Direct chats) is the design.

A person has one direct chat with each agent, held by the key chats_direct_key of (person, agent): two requests at once
make one chat, the second inserting nothing and finding the first's. Making one takes the checks of adding the agent to
a chat (``chat_members.addable_agent``: 404 for an agent the caller cannot see, 403 without the writer role on its
project, 409 for a deleted one), a person and an agent of different names, and the caller clearing the label of the
agent's project (409 otherwise). A direct chat there is opens without them, so the one of an agent deleted since still
reads; ``chat_access`` decides who reads it, as for any chat. Its title is the agent's name when it was made; it is
audited as chat.create in the agent's project, naming the chat and the agent.

Its members never change (``chat_members.refuse_direct``). A message of its person that mentions no agent of the chat is
work for its agent, as if it mentioned it (``direct_mention``, from ``chats``).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, tables
from evo_agents.hub.server import audit
from evo_agents.hub.server.chat_access import ChatAccess, chat_access, projects_by_id, uncleared
from evo_agents.hub.server.chat_members import REFUSALS, HandlePath, addable_agent, refuse_clash, refuse_uncleared
from evo_agents.hub.server.chat_store import add_member, member_event
from evo_agents.hub.server.chat_views import Chat, agent_author, chat_view, person_author
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.profile_store import Profile, by_handle
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})


async def _found(conn: AsyncConnection, user_id: int, profile_id: int) -> int | None:
    c = tables.chats
    query = select(c.c.id).where(c.c.direct_user_id == user_id, c.c.direct_profile_id == profile_id)
    return (await conn.execute(query)).scalar_one_or_none()


async def _make(conn: AsyncConnection, user: Principal, profile: Profile) -> int | None:
    """Make the direct chat of ``user`` and agent ``profile``, with both in it, its events and its audit row; its id,
    or None when another request made it first (its transaction committed, so ``_found`` finds it)."""
    c = tables.chats
    values = {"title": profile.name[: chats.MAX_TITLE_CHARS], "created_by": user.user_id, "kind": chats.DIRECT}
    statement = (
        pg_insert(c)
        .values(**values, direct_user_id=user.user_id, direct_profile_id=profile.id)
        .on_conflict_do_nothing(index_elements=[c.c.direct_user_id, c.c.direct_profile_id])
        .returning(c.c.id)
    )
    chat_id = (await conn.execute(statement)).scalar_one_or_none()
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
    found = None if profile is None else await _found(conn, user.user_id, profile.id)
    if found is not None:
        return found, False
    profile = await addable_agent(conn, user, handle)
    refuse_clash([user.login], [profile.handle])
    projects = await projects_by_id(conn, [profile.project_id])
    refuse_uncleared(await uncleared(conn, {user.user_id: user.login}, projects), f"a direct chat with @{handle}")
    made = await _make(conn, user, profile)
    if made is not None:
        return made, True
    return (await _found(conn, user.user_id, profile.id)), False


@router.put(
    "/direct/{handle}",
    response_model=Chat,
    responses={201: {"model": Chat, "description": "the direct chat, made now"}, **REFUSALS},
)
async def open_direct(request: Request, response: Response, handle: HandlePath, user: CurrentUser) -> Chat:
    """The caller's direct chat with agent ``handle``: the one there is (200), or one made now (201)."""
    async with request.app.state.engine.begin() as conn:
        chat_id, made = await direct_chat(conn, user, handle)
        view = await chat_view(conn, await chat_access(conn, user, chat_id))
    response.status_code = 201 if made else 200
    log.info("direct chat opened", extra={"chat_id": chat_id, "handle": handle, "made": made})
    return view


async def direct_mention(conn: AsyncConnection, chat: ChatAccess, kept: list[dict]) -> list[dict]:
    """The mention of the agent of ``chat``, a direct or a settle chat (``chats.ATTENDED``), ``via`` the chat's kind,
    for a message whose mentions ``kept`` do not reach it; none when they do, so that a message makes one run of it at
    most."""
    c, ap = tables.chats, tables.agent_profiles
    query = select(ap.c.id, ap.c.handle).join_from(c, ap, ap.c.id == c.c.direct_profile_id)
    row = (await conn.execute(query.where(c.c.id == chat.chat_id))).one()
    if any(mention["profile_id"] == row.id for mention in kept):
        return []
    return [{"name": row.handle, "via": chat.kind, "user_id": None, "profile_id": row.id, "refusal": None}]
