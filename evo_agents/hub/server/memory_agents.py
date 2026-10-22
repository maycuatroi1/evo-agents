"""The memories of agents and the fences of memories in a run (plan agent-profile-workspace, criteria a1 and a2), for
``evo_agents.hub.server.memories``.

Namespaces: a memory of a project belongs to no agent (``memories.profile_id`` NULL: the project's shared memories and
a person's own, as before) or to one agent profile of the project that is not its default. The agent of a run under
such a profile (``RunScope.agent``) writes into its profile's namespace alone, and reads the shared memories with its
own, never another profile's. A run under the default profile reads and writes as before. A member reads the memories
of every agent of a project under the project's read rule, whatever their type, by id or by naming the agent
(``?agent=``); a listing or a search that names no agent leaves them out, so ``evo-agents hub memory pull`` never
brings them to a machine. A writer changes and deletes them like any memory of the project.

Fences of a run, each a 403 whose message the agent reads through /mcp:

- an agent under a profile that is not its project's default writes no shared memory: neither one at a name a shared
  memory holds, nor a deletion of one; the Curator considers promoting an agent's memories into the shared ones;
- a chat run, under any profile, neither reads nor writes memories of type user (the person's own); feedback stays
  readable; an agent's own memories are never of type user;
- a run at hop 2 or more (work an agent handed to another agent) writes no memory at all;
- one run writes at most MAX_RUN_WRITES memories; changing one it wrote already is no new one.

Provenance: each revision a run writes records the run, the profile and version it runs under, the person at its
origin (the run's owner, runs.dispatched_by), its hop, and its chat and chat message (``provenance``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Integer, SmallInteger, and_, bindparam, or_, select, true
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables, tables_memory_agents
from evo_agents.hub.db import one_of
from evo_agents.hub.profiles import HANDLE_PATTERN
from evo_agents.hub.server.security import Principal, RunScope

MAX_RUN_WRITES = 5  # memories one run writes
NONE, OWN, BOTH = "none", "own", "both"  # the namespaces a read reaches: no agent's, one agent's, or both
LIVE = "live"  # what a run's agent reaches naming its own namespace: its memories less those archived
AGENT_TYPES = ("project", "reference", "feedback")  # what an agent's memory may be

AgentHandle = Annotated[
    str | None,
    Query(
        pattern=HANDLE_PATTERN,
        description="only the memories of this agent profile (its handle, @ left out); left out, those of no agent",
    ),
]


class Provenance(BaseModel):
    """Where a revision came from, when the agent of a run wrote it."""

    run: int = Field(description="the run whose agent wrote it")
    agent: str | None = Field(description="the handle of the profile the run ran under")
    agent_version: int | None = Field(description="the version of that profile")
    origin: str | None = Field(description="the login of the person at the run's origin, who dispatched it")
    hop: int | None = Field(description="the run's hop in its chat: 1 for a person's mention, more agent to agent")
    chat: int | None = Field(description="the chat of the run")
    chat_message: int | None = Field(description="the chat message that started the run")


def _chat(scope: RunScope | None) -> bool:
    return scope is not None and (scope.chat_id is not None or scope.kind == "chat")


def readable(profile_id: int | None, kind: str, user: Principal) -> bool:
    """The part of the read rule that namespaces and chats add: a run's agent reads no other namespace than its own
    (none for a default profile) and, in a chat run, no memory of type user."""
    scope = user.scope
    if scope is None:
        return True
    if profile_id is not None and profile_id != scope.namespace:
        return False
    return not (_chat(scope) and kind == "user")


# What a listing or a search reaches: the namespaces, then the conditions on memories.


async def _profile(conn: AsyncConnection, handle: str) -> tuple[int, str, bool] | None:
    ap, p = tables.agent_profiles, tables.projects
    found = select(ap.c.id, p.c.name, ap.c.is_default).join_from(ap, p, p.c.id == ap.c.project_id)
    row = (await conn.execute(found.where(ap.c.handle == handle))).first()
    return None if row is None else tuple(row)


def _no_agent(handle: str) -> HTTPException:
    return HTTPException(404, f"no agent @{handle} with memories of its own in a project you can read")


async def reach(
    conn: AsyncConnection, user: Principal, agent: str | None, project: str | None
) -> tuple[str, int | None, str | None]:
    """The namespaces a listing or a search of ``user`` reaches, the profile id they name, and the project to narrow
    to (the agent's, when one is named). 404 when ``agent`` is no agent of a project the caller could read, a default
    profile, or another profile than a run's own."""
    scope = user.scope
    if agent is None:
        return (BOTH if scope is not None and scope.namespace else NONE), scope and scope.namespace, project
    found = await _profile(conn, agent)
    if found is None or found[2] or (project is not None and project != found[1]):
        raise _no_agent(agent)
    if scope is not None and found[0] != scope.namespace:
        raise _no_agent(agent)
    return (OWN if scope is None else LIVE), found[0], found[1]


def namespace_condition(namespace: str):
    """The condition on memories.profile_id of a listing or a search reaching ``namespace``, :namespace_id bound. The
    agent of a run (BOTH, LIVE) reaches none of its memories the daily upkeep archived."""
    m = tables.memories
    named = m.c.profile_id == bindparam("namespace_id", type_=BigInteger)
    live = and_(named, m.c.archived_at.is_(None))
    none = m.c.profile_id.is_(None)
    return {NONE: none, OWN: named, LIVE: live, BOTH: or_(none, live)}[namespace]


def key_condition(kind: str):
    """The namespace of the memory at a key, by ``kind`` of key: an agent's (:profile_id), else none."""
    m = tables.memories
    if kind == "agent":
        return m.c.profile_id == bindparam("profile_id", type_=BigInteger)
    return m.c.profile_id.is_(None)


def key_kind(kind: str) -> object:
    """What else the key of ``kind`` asks of the memory there: shared, the caller's (:user_id) own, or anything in the
    agent's namespace."""
    m = tables.memories
    if kind == "agent":
        return true()
    if kind == "shared":
        return one_of(m.c.type, tables_memory_agents.SHARED)
    return and_(one_of(m.c.type, tables_memory_agents.OWNED), m.c.owner_id == bindparam("user_id", type_=BigInteger))


# Writes


def _refused(message: str) -> HTTPException:
    return HTTPException(403, f"{message}; nothing was written")


def promotion(scope: RunScope, project: str) -> str:
    return (
        f"the agent of run {scope.run_id} (@{scope.agent}) writes its memories into its own namespace in project "
        f"{project}, never the project's shared memories: the Curator considers promoting an agent's memories into them"
    )


async def namespace(conn: AsyncConnection, user: Principal, project_id: int, project: str, agent: str | None):
    """The namespace a write of ``user`` naming ``agent`` (a handle, or None) goes into: the profile id, or None for
    the shared memories and one's own. A run's agent under a profile that is not its project's default writes into its
    own alone, one under the default into none; a member names the agent. 403 or 422 when that cannot be."""
    scope = user.scope
    if scope is not None and scope.agent is not None:
        if agent not in (None, scope.agent):
            raise _refused(f"the agent of run {scope.run_id} writes into the memories of @{scope.agent} alone")
        return scope.profile_id
    if agent is None:
        return None
    if scope is not None:
        raise _refused(f"the agent of run {scope.run_id} runs under the default profile and writes no agent's memory")
    found = await _profile(conn, agent)
    if found is None or found[1] != project or found[2]:
        raise HTTPException(
            422, f"project {project} has no agent @{agent} other than its default profile, which keeps no memory"
        )
    return found[0]


_WRITTEN = (
    select(tables.memory_revisions.c.memory_id)
    .distinct()
    .where(tables.memory_revisions.c.run_id == bindparam("run_id", type_=BigInteger))
    .limit(MAX_RUN_WRITES + 1)
)  # the memories run :run_id wrote


async def check_write(
    conn: AsyncConnection, user: Principal, kind: str, memory_id: int | None, profile_id: int | None, project: str
) -> None:
    """The fences of a run on a write of memory ``memory_id`` (None for a new one) of type ``kind`` in namespace
    ``profile_id``: 403 with what the agent reads when it may not. Nothing for a principal of no run."""
    scope = user.scope
    if scope is None:
        return
    if (scope.hop or 1) >= 2:
        raise _refused(
            f"run {scope.run_id} is at hop {scope.hop}, work an agent handed to another agent, and such a run writes "
            "no memory: say what is worth remembering in the chat"
        )
    if scope.agent is not None and profile_id != scope.namespace:
        raise _refused(promotion(scope, project))
    if _chat(scope) and kind == "user":
        raise _refused(
            f"the agent of chat run {scope.run_id} neither reads nor writes memories of type user: those are the "
            "person's own (feedback stays readable)"
        )
    if scope.agent is not None and kind not in AGENT_TYPES:
        raise _refused(f"a memory of @{scope.agent} is of type project, reference or feedback, not {kind}")
    written = set((await conn.execute(_WRITTEN, {"run_id": scope.run_id})).scalars())
    if memory_id not in written and len(written) >= MAX_RUN_WRITES:
        raise _refused(
            f"run {scope.run_id} wrote {MAX_RUN_WRITES} memories already, as many as one run may: change one of "
            "those instead"
        )


def provenance(user: Principal) -> dict:
    """The provenance columns of a revision ``user`` writes, as the parameters of the revision row (``prov_*``)."""
    scope = user.scope
    if scope is None:
        return dict.fromkeys(PARAMETERS)
    values = (
        scope.run_id,
        scope.profile_id,
        scope.profile_version,
        user.user_id,
        scope.hop,
        scope.chat_id,
        scope.chat_message_id,
    )
    return dict(zip(PARAMETERS, values, strict=True))


# memory_revisions column: (parameter of the revision row, its type)
COLUMNS = {
    "run_id": ("prov_run", BigInteger),
    "profile_id": ("prov_profile", BigInteger),
    "profile_version": ("prov_version", Integer),
    "origin_id": ("prov_origin", BigInteger),
    "hop": ("prov_hop", SmallInteger),
    "chat_id": ("prov_chat", BigInteger),
    "chat_message_id": ("prov_message", BigInteger),
}
PARAMETERS = tuple(parameter for parameter, _ in COLUMNS.values())


def provenance_parameters() -> list:
    """The bind parameters of the provenance columns, in the order of COLUMNS."""
    return [bindparam(parameter, type_=kind) for parameter, kind in COLUMNS.values()]


def provenance_columns():
    """The columns of memory_revisions a revision's provenance shows, labelled ``prov_*``, and the joins they need
    from memory_revisions: (columns, join)."""
    r, ap = tables.memory_revisions, tables.agent_profiles
    origin = tables.users.alias("origin")
    columns = (
        r.c.run_id.label("prov_run"),
        ap.c.handle.label("prov_agent"),
        r.c.profile_version.label("prov_version"),
        origin.c.login.label("prov_origin"),
        r.c.hop.label("prov_hop"),
        r.c.chat_id.label("prov_chat"),
        r.c.chat_message_id.label("prov_message"),
    )

    def join(source):
        return source.outerjoin(ap, ap.c.id == r.c.profile_id).outerjoin(origin, origin.c.id == r.c.origin_id)

    return columns, join


def provenance_of(values) -> Provenance | None:
    """The provenance of a revision row of ``provenance_columns``; None when no run wrote it."""
    v = values._mapping
    if v["prov_run"] is None:
        return None
    return Provenance(
        run=v["prov_run"],
        agent=v["prov_agent"],
        agent_version=v["prov_version"],
        origin=v["prov_origin"],
        hop=v["prov_hop"],
        chat=v["prov_chat"],
        chat_message=v["prov_message"],
    )
