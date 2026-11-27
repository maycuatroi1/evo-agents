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

from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub.server.agents.service import tasking as agent_tasking
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.chats.models.home import ChatHomeAgent
from evo_agents.hub.server.chats.repository import home as store
from evo_agents.hub.server.profile_store import icon_url
from evo_agents.hub.server.security import Principal


async def chat_agents(engine: AsyncEngine, user: Principal) -> list[ChatHomeAgent]:
    """The agents ``user`` may chat with, as the module's docstring says, in its order."""
    async with UnitOfWork(engine) as conn:
        candidates = await store.candidates(conn, user.user_id)
        working = await agent_tasking.takes_work(conn, [profile.id for profile in candidates])
        chats = await store.direct_chats(conn, user.user_id)
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
