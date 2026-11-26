"""Where the chat runs of an agent go when its agent type says so (``docs/plugins.md``, Agent types), and who a chat
run works for.

An agent type whose manifest declares ``chat_runs: run_as`` (RUN_AS) sends every chat run of its profile to a worker
of the member the project enabled the type under (``run_as``), to the nodes of the agent's pool, whoever mentioned
the agent: a member without a worker reaches it too. The run's row is dispatched by that member, whose
credentials it leases and whose grant the agent's session on the hub's /mcp carries; the person who
mentioned the agent stays the origin of the chain, read from the message that started the run (``origin``), and the
hub's chat code acts for that person as before. Every other profile, and that one while its type or its plugin is
turned off in the project, routes as it did: to a worker of the person who mentioned it (MENTIONER, the default).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server.chats.models.route import RUN_AS, Route, chat_runs_of
from evo_agents.hub.server.chats.repository import route as store


async def route_of(conn: AsyncConnection, profile_id: int) -> Route | None:
    """The route of the chat runs of profile ``profile_id``: that of the agent type its project enabled it for, while
    the type is on, its plugin enabled and on in the project, and the installed version declares RUN_AS; else None."""
    for row in await store.agent_types_of(conn, profile_id):
        if chat_runs_of(row.manifest, row.agent_type) == RUN_AS:
            return Route(row.agent_type, row.run_as, row.run_as_login)
    return None


async def origin(conn: AsyncConnection, run_id: int) -> int | None:
    """The person at the origin of the chain of chat run ``run_id``: the author of the message that started it, or,
    for a message of an agent, the origin of that agent's run (its dispatcher, for a run of another kind); None when
    the message is the hub's own, for the run's dispatcher to stand. Hops have no limit, so it walks back as far as
    the chain goes, each run once."""
    current, seen = run_id, set()
    while current not in seen:
        seen.add(current)
        row = await store.run_origin(conn, current)
        if row is None:
            return None
        if row.kind != "chat":
            return row.dispatched_by
        if row.author_user_id is not None:
            return row.author_user_id
        if row.run_id is None:
            return None
        current = row.run_id
    return None
