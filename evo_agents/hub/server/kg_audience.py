"""The audience a reader of a project's knowledge graph holds on the hub (``evo_agents.kg.policy``), computed from the
database at every read, so a change of members or grants counts at the next read, with no build.

A person holds:

- ``chat:<id>`` for each chat they are a member of, not removed, whose label as it is now they clear: the chats
  ``chat_access`` lets them read (``chat_access.cleared_chats``);
- ``grant:<project>:<level>`` for each project they hold a grant on and each level of that project's ladder up to the
  grant's max level: what ``LabelReader.why_not`` asks of a label naming that project at that level. A max level the
  ladder lacks gives none.

The agent of a run (a principal with a ``RunScope``, on /mcp) holds the audience of the run's owner, the person the
principal is. The agent of a chat run holds what both people its prompt is cleared for hold
(``chat_runs.run_clearers``): the person who started it and the member whose worker runs it, when another.

Every graph read of the hub asks this module: the kg_* tools of REST and /mcp (``kg.tool_result``), kg_more on /mcp,
and the web's graph pages (``kg_web``); the retrieval eval of the night's measure and of the optimize loop reads with
the audience of the night shift's owner, as their agents do (``kg_health``, ``kg_optimize``). The review of a proposal reads with no audience instead: what it keeps is
shown to every member of the project (``proposals.graph_view``).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.chat_access import cleared_chats
from evo_agents.hub.server.chat_runs import run_clearers
from evo_agents.hub.server.security import Principal

CHAT_RUN = "chat"  # the kind of a chat run (``runs.kind``)


async def person_audience(conn: AsyncConnection, user_id: int) -> frozenset[str]:
    """The audience person ``user_id`` holds now (see the module)."""
    g, p = tables.grants, tables.projects
    query = select(p.c.name, p.c.levels, g.c.max_level).join_from(g, p, p.c.id == g.c.project_id)
    found = set()
    for name, levels, max_level in await conn.execute(query.where(g.c.user_id == user_id)):
        levels = list(levels)
        if max_level in levels:
            found.update(f"grant:{name}:{level}" for level in levels[: levels.index(max_level) + 1])
    found.update(f"chat:{chat_id}" for chat_id in await cleared_chats(conn, user_id))
    return frozenset(found)


async def reader_audience(conn: AsyncConnection, user: Principal) -> frozenset[str]:
    """The audience ``user`` reads a knowledge graph with now: its person's, or for the agent of a chat run, what both
    people the run is cleared for hold."""
    scope = user.scope
    return await agent_audience(conn, user.user_id, scope.run_id if scope else None, scope.kind if scope else None)


async def agent_audience(conn: AsyncConnection, owner_id: int, run_id: int | None, kind: str | None) -> frozenset[str]:
    """The audience the agent of run ``run_id`` of ``kind``, owned by ``owner_id``, reads with now: what both people a
    chat run is cleared for hold, else its owner's; the owner's alone without a run."""
    people = [owner_id]
    if run_id is not None and kind == CHAT_RUN:
        people = await run_clearers(conn, run_id) or people
    audiences = [await person_audience(conn, person) for person in people]
    return frozenset.intersection(*audiences)
