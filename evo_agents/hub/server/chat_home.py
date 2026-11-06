"""The agents a person may chat with, for the chat home of the web: GET /v1/me/chat-agents.

The list holds every agent of every project the caller holds a grant on that they may give work to and that takes
work, by the rules a mention in a chat and an agent asking another agent share (``agent_tasking``): it takes work while
it is not deleted and has a pool of its own (``takes_work``), and the caller may give it work while they hold writer on
its project, and admin for an agent that takes work from the admins alone (``task_refusal``). A settle chat's agent,
which any member of its project may give work in that chat, is listed only by those rules, since the home opens a
direct chat, never a settle chat. Each agent comes with what the home shows of it (name, description, colour, avatar,
starters), the caller's direct chat with it when there is one, and when the caller last wrote in that chat; the ones
written to last come first, then by project, the default agent of a project first, then by handle.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server import agent_tasking
from evo_agents.hub.server.agent_profiles import icon_url
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.profile_store import Profile, profile_of, profile_select
from evo_agents.hub.server.security import CurrentUser, Principal

router = APIRouter(prefix="/v1/me", tags=["chats"], responses={401: {"model": ErrorBody}})


class ChatHomeAgent(BaseModel):
    handle: str
    project: str
    name: str
    description: str
    color: str
    icon_url: str | None = Field(description="GET of the avatar on this hub; null: draw the initial on the colour")
    starters: list[str] = Field(description="questions to offer before a first message, in order")
    is_default: bool = Field(description="the project's default agent, @<project>")
    chat_id: int | None = Field(description="your direct chat with the agent; null until one is opened")
    last_chatted_at: datetime | None = Field(
        description="when you last wrote in your direct chat with the agent; null when you never did"
    )


async def _candidates(conn: AsyncConnection, user_id: int) -> list[Profile]:
    """The agents not deleted of the projects ``user_id`` holds a grant on."""
    ap, g = tables.agent_profiles, tables.grants
    projects = select(g.c.project_id).where(g.c.user_id == user_id)
    query = profile_select().where(ap.c.project_id.in_(projects), ap.c.deleted_at.is_(None))
    return [profile_of(row) for row in await conn.execute(query)]


async def _direct_chats(conn: AsyncConnection, user_id: int) -> dict[int, tuple[int, datetime | None]]:
    """{agent id: (the id of ``user_id``'s direct chat with it, when they last wrote in it)}."""
    c, m = tables.chats, tables.chat_messages
    last = (
        select(func.max(m.c.created_at)).where(m.c.chat_id == c.c.id, m.c.author_user_id == user_id).scalar_subquery()
    )
    query = select(c.c.direct_profile_id, c.c.id, last.label("last")).where(c.c.direct_user_id == user_id)
    return {row.direct_profile_id: (row.id, row.last) for row in await conn.execute(query)}


async def chat_agents(conn: AsyncConnection, user: Principal) -> list[ChatHomeAgent]:
    """The agents ``user`` may chat with, as the module's docstring says, in its order."""
    candidates = await _candidates(conn, user.user_id)
    working = await agent_tasking.takes_work(conn, [profile.id for profile in candidates])
    chats = await _direct_chats(conn, user.user_id)
    found = []
    for profile in candidates:
        if profile.id not in working:
            continue
        if await agent_tasking.task_refusal(conn, profile, user.user_id, user.login) is not None:
            continue
        chat_id, last = chats.get(profile.id, (None, None))
        found.append(
            ChatHomeAgent(
                handle=profile.handle,
                project=profile.project,
                name=profile.name,
                description=profile.description,
                color=profile.color,
                icon_url=icon_url(profile),
                starters=list(profile.starters or ()),
                is_default=profile.is_default,
                chat_id=chat_id,
                last_chatted_at=last,
            )
        )
    found.sort(key=lambda agent: (agent.project, not agent.is_default, agent.handle))
    found.sort(key=lambda agent: -agent.last_chatted_at.timestamp() if agent.last_chatted_at else float("inf"))
    return found


@router.get("/chat-agents", response_model=list[ChatHomeAgent])
async def list_chat_agents(request: Request, user: CurrentUser) -> list[ChatHomeAgent]:
    """The agents you may chat with on any project you hold a grant on, the ones you wrote to last first."""
    async with request.app.state.engine.begin() as conn:
        return await chat_agents(conn, user)
