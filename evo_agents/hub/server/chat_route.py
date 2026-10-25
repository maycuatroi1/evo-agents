"""Where the chat runs of an agent go when its agent type says so (``docs/plugins.md``, Agent types), and who a chat
run works for.

An agent type whose manifest declares ``chat_runs: run_as`` (RUN_AS) sends every chat run of its profile to a worker
of the member the project enabled the type under (``run_as``), among the workers the type names, whoever mentioned
the agent: a member without a worker reaches it too. The run's row is dispatched by that member, whose worker claims
it, whose credentials it leases and whose grant the agent's session on the hub's /mcp carries; the person who
mentioned the agent stays the origin of the chain, read from the message that started the run (``origin``), and the
hub's chat code acts for that person as before. Every other profile, and that one while its type or its plugin is
turned off in the project, routes as it did: to a worker of the person who mentioned it (MENTIONER, the default).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chat_runs as rules
from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import enabled_query

RUN_AS = "run_as"  # the type's chat runs go to the workers of its run_as
MENTIONER = "mentioner"  # the default: to the workers of the person who mentioned the agent
CHAT_RUNS = (MENTIONER, RUN_AS)  # plugin.schema.json, $defs.agent_type.chat_runs


@dataclass(frozen=True)
class Route:
    """The member whose workers take the chat runs of a profile, ``run_as`` of agent type ``agent_type``: their id and
    login, and the names of the workers the type names (none: any the profile allows)."""

    agent_type: str
    user_id: int
    login: str
    workers: tuple[str, ...] = ()

    def named(self, name: str) -> bool:
        """Whether worker ``name`` is one the type lets take its runs."""
        return not self.workers or name.lower() in self.workers


def chat_runs_of(manifest: dict, name: str) -> str:
    """Where the chat runs of agent type ``name`` of ``manifest`` (as stored) go: RUN_AS or MENTIONER."""
    for item in (manifest.get("contributes") or {}).get("agent_types") or ():
        if item.get("name") == name:
            return item.get("chat_runs") or MENTIONER
    return MENTIONER


async def route_of(conn: AsyncConnection, profile_id: int) -> Route | None:
    """The route of the chat runs of profile ``profile_id``: that of the agent type its project enabled it for, while
    the type is on, its plugin enabled and on in the project, and the installed version declares RUN_AS; else None."""
    t, u = tables.project_agent_types, tables.users
    query = (
        enabled_query()
        .add_columns(t.c.name.label("agent_type"), t.c.run_as, u.c.login.label("run_as_login"), t.c.workers)
        .join(t, (t.c.plugin_id == tables.plugins.c.id) & (t.c.project_id == tables.projects.c.id))
        .join(u, u.c.id == t.c.run_as)
        .where(t.c.profile_id == profile_id, t.c.disabled_at.is_(None))
    )
    for row in await conn.execute(query):
        if chat_runs_of(row.manifest, row.agent_type) == RUN_AS:
            workers = tuple(name.lower() for name in row.workers or ())
            return Route(row.agent_type, row.run_as, row.run_as_login, workers)
    return None


async def origin(conn: AsyncConnection, run_id: int) -> int | None:
    """The person at the origin of the chain of chat run ``run_id``: the author of the message that started it, or,
    for a message of an agent, the origin of that agent's run (its dispatcher, for a run of another kind); None when
    the message is the hub's own, for the run's dispatcher to stand. At most MAX_HOPS steps back."""
    r, m = tables.runs, tables.chat_messages
    current = run_id
    for _ in range(rules.MAX_HOPS + 1):
        query = (
            select(r.c.kind, r.c.dispatched_by, m.c.author_user_id, m.c.run_id)
            .select_from(r.outerjoin(m, m.c.id == r.c.chat_message_id))
            .where(r.c.id == current)
        )
        row = (await conn.execute(query)).one_or_none()
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


__all__ = ["CHAT_RUNS", "MENTIONER", "RUN_AS", "Route", "chat_runs_of", "origin", "route_of"]
