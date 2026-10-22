"""The index of an agent's memories in the prompt of its runs, and their upkeep (plan agent-profile-workspace,
criterion a3).

The index: the prompt of a run under an agent profile that is not its project's default ends with the memories of that
profile, newest first: each one's name, id, type, description and a stale mark, at most INDEX_BYTES and within the room
the prompt of the run's kind leaves under MAX_PROMPT_BYTES (``with_index``). It lists only what the agent of the run
could open with memory_get: a memory neither deleted nor archived, whose label the run's owner reads through the
agent's sink (``AGENT_SINK``) and that names no audience. A profile without such a memory, and the default profile,
leave the prompt as it was.

The upkeep, the daily job ``hub.memory_upkeep`` (``upkeep``): an agent's memory nobody opened for EXPIRE_AFTER (the
agent of a run opening it with memory_get, ``memory_state.opened``; a new revision counts too) is archived: out of the
index and of the searches and listings of runs (``memory_agents.namespace_condition``), still shown to members, until a
writer of its project restores it (POST /v1/memories/{id}/restore, ``evo-agents hub memory restore``). An agent's
memory that cites code as the Reviewer's evidence does (``code:REPO:PATH[:LINE]``, ``review.parse_evidence``) is stale
while the project's latest knowledge graph has no file at one of those paths, read as the person who owns the memory
(``change_tiers.impact_as_owner``); a project without a graph, or whose graph cannot be read now, keeps the marks it
had. A memory the Curator promoted into a shared one (``memory_promotion``) is out of the index, and the job leaves it
alone.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import BigInteger, DateTime, bindparam, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.memory import AGENT_SINK
from evo_agents.hub.review import EvidenceProblem, parse_evidence
from evo_agents.hub.runs import MAX_PROMPT_BYTES
from evo_agents.hub.server import memories
from evo_agents.hub.server.audit import record
from evo_agents.hub.server.change_tiers import GraphReader, impact_as_owner
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg_health import owner_access
from evo_agents.hub.server.mcp_text import memory_about
from evo_agents.hub.server.memory_audience import label_audience
from evo_agents.hub.server.memory_paging import BoundedRoute
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

INDEX_BYTES = 4 * 1024  # the index of an agent's memories in a prompt, at most
INDEX_ROWS = 200  # memories read for an index, newest first
EXPIRE_AFTER = timedelta(days=30)  # an agent's memory nobody opened for this long is archived
RESTORE = "memory.restore"  # audit action; the target is memory:<id>
CITATION = re.compile(r"\bcode:[A-Za-z0-9][A-Za-z0-9._-]*:[^\s`'\"<>()\[\]{}|,;]+")
TRAILING = ".:"  # punctuation a sentence leaves after a citation


# The index


_INDEX_RUN = (
    select(
        tables.agent_profiles.c.id.label("profile_id"),
        tables.agent_profiles.c.handle,
        tables.projects.c.name.label("project"),
        tables.users.c.id.label("owner_id"),
        tables.users.c.login,
    )
    .join_from(tables.runs, tables.agent_profiles, tables.agent_profiles.c.id == tables.runs.c.profile_id)
    .join(tables.projects, tables.projects.c.id == tables.runs.c.project_id)
    .join(tables.users, tables.users.c.id == tables.runs.c.dispatched_by)
    .where(tables.runs.c.id == bindparam("run_id", type_=BigInteger), ~tables.agent_profiles.c.is_default)
)  # the profile of run :run_id, when not its project's default, its project and the person who dispatched it

_INDEXED = (
    select(
        tables.memories.c.id,
        tables.memories.c.name,
        tables.memories.c.type,
        tables.memories.c.label,
        tables.memories.c.body,
        tables.memories.c.stale_at,
    )
    .where(
        tables.memories.c.profile_id == bindparam("profile_id", type_=BigInteger),
        ~tables.memories.c.deleted,
        tables.memories.c.archived_at.is_(None),
        tables.memories.c.promoted_at.is_(None),
    )
    .order_by(tables.memories.c.updated_at.desc(), tables.memories.c.id.desc())
    .limit(INDEX_ROWS)
)  # the memories of profile :profile_id an index may list, newest first


def _size(lines: list[str]) -> int:
    return len("\n".join(lines).encode()) + 1


async def memory_index(conn: AsyncConnection, run_id: int, room: int = INDEX_BYTES) -> str:
    """The index of the memories of run ``run_id``'s agent profile, within ``room`` and INDEX_BYTES bytes; empty for a
    run under its project's default profile or none, and for a profile with no memory its agent could open."""
    run = (await conn.execute(_INDEX_RUN, {"run_id": run_id})).one_or_none()
    if run is None:
        return ""
    access = await owner_access(conn, run.owner_id, run.login, run.project)
    if access is None:
        return ""
    head = [
        f"# Memories of @{run.handle}",
        f"What you, @{run.handle}, noted in project {run.project}, newest first: names and descriptions only. Open "
        "one with memory_get and its id before relying on it; a stale one cites code the project's knowledge graph no "
        "longer has. A memory_write under your handle adds to them.",
    ]
    budget, lines = min(room, INDEX_BYTES), []
    for memory in (await conn.execute(_INDEXED, {"profile_id": run.profile_id})).all():
        if label_audience(memory.label) or not access.visible(memory.label, AGENT_SINK):
            continue
        stale = ", stale" if memory.stale_at is not None else ""
        line = f"- {memory.name} (id {memory.id}, {memory.type}{stale}): {memory_about(memory.body)}"
        if _size([*head, *lines, line]) > budget:
            break
        lines.append(line)
    return "\n".join([*head, *lines]) + "\n" if lines else ""


async def with_index(conn: AsyncConnection, run_id: int, prompt: str) -> str:
    """``prompt``, the prompt of run ``run_id``, with the index of its agent's memories at its end when it has one,
    within MAX_PROMPT_BYTES whole; ``prompt`` as it is otherwise."""
    index = await memory_index(conn, run_id, MAX_PROMPT_BYTES - len(prompt.rstrip("\n").encode()) - 2)
    return prompt.rstrip("\n") + "\n\n" + index if index else prompt


# The upkeep


def citations(body: str) -> set[str]:
    """The paths ``body`` cites as ``code:REPO:PATH[:LINE]``, each as ``REPO:PATH``."""
    found = set()
    for match in CITATION.finditer(body or ""):
        try:
            item = parse_evidence(match.group(0).rstrip(TRAILING))
        except EvidenceProblem:
            continue
        found.add(f"{item['repo']}:{item['path']}")
    return found


_LIVE = (
    tables.memories.c.profile_id.is_not(None),
    ~tables.memories.c.deleted,
    tables.memories.c.promoted_at.is_(None),
)  # the memories of agents, neither deleted nor promoted

_ARCHIVED = (
    update(tables.memories)
    .values(archived_at=bindparam("now", type_=DateTime(timezone=True)))
    .where(
        *_LIVE,
        tables.memories.c.archived_at.is_(None),
        func.coalesce(tables.memories.c.read_at, tables.memories.c.created_at)
        < bindparam("cutoff", type_=DateTime(timezone=True)),
        tables.memories.c.updated_at < bindparam("cutoff", type_=DateTime(timezone=True)),
    )
)  # the memories of agents nobody opened or wrote since :cutoff, archived at :now

_CITING = select(
    tables.memories.c.id,
    tables.memories.c.project_id,
    tables.memories.c.owner_id,
    tables.memories.c.body,
    tables.memories.c.stale_at,
).where(*_LIVE)

_MARKED = (
    update(tables.memories)
    .values(stale_at=bindparam("stale", type_=DateTime(timezone=True)))
    .where(tables.memories.c.id == bindparam("marked_id", type_=BigInteger))
)


async def _mark_stale(engine: AsyncEngine, reader: GraphReader, now: datetime) -> dict:
    """The stale marks of the agents' memories, by the graph of each project as each memory's owner reads it."""
    async with engine.begin() as conn:
        rows = (await conn.execute(_CITING)).all()
    groups: dict[tuple[int, int], list] = {}
    for row in rows:
        groups.setdefault((row.project_id, row.owner_id), []).append((row, citations(row.body)))
    counts = {"stale": 0, "fresh": 0, "unread": 0}
    for (project_id, owner_id), cited in groups.items():
        targets = sorted(set().union(*(paths for _, paths in cited)))
        graph = await impact_as_owner(engine, project_id, targets, reader, owner_id=owner_id) if targets else None
        if graph is None and targets:
            counts["unread"] += len(cited)
            continue
        marks = []
        for row, paths in cited:
            gone = bool(graph is not None and paths & graph.unmatched)
            if gone and row.stale_at is None:
                marks.append({"marked_id": row.id, "stale": now})
                counts["stale"] += 1
            elif not gone and row.stale_at is not None:
                marks.append({"marked_id": row.id, "stale": None})
                counts["fresh"] += 1
        if marks:
            async with engine.begin() as conn:
                await conn.execute(_MARKED, marks)
    return counts


async def upkeep(engine: AsyncEngine, blobs, data_dir: Path | None, *, now: datetime | None = None) -> dict:
    """The daily upkeep of the agents' memories: those nobody opened for EXPIRE_AFTER archived, and the stale marks of
    those that cite code set by the projects' graphs (``blobs`` and the cache under ``data_dir``)."""
    now = now or datetime.now(UTC)
    async with engine.begin() as conn:
        archived = (await conn.execute(_ARCHIVED, {"now": now, "cutoff": now - EXPIRE_AFTER})).rowcount
    counts = {"archived": archived, **await _mark_stale(engine, GraphReader.at(blobs, data_dir), now)}
    log.info("agent memories kept", extra=counts)
    return counts


# Restoring an archived memory

router = APIRouter(
    prefix="/v1/memories", tags=["memories"], route_class=BoundedRoute, responses={401: {"model": ErrorBody}}
)

_RESTORED = (
    update(tables.memories)
    .values(archived_at=None, read_at=func.now())
    .where(tables.memories.c.id == bindparam("restored_id", type_=BigInteger))
)


@router.post("/{memory_id}/restore", response_model=memories.Memory, responses=memories.REFUSALS)
async def restore_memory(
    request: Request, memory_id: memories.MemoryId, user: CurrentUser, sink: memories.Sink = AGENT_SINK
) -> memories.Memory:
    """Bring back an agent's memory the daily upkeep archived: into the index and the searches of its agent's runs
    again, as if just opened, with the audit row ``memory.restore``. A writer of its project who reads it, never the
    agent of a run; a memory not archived is left as it is."""
    async with request.app.state.engine.begin() as conn:
        row, access = await memories._readable(conn, user, memory_id, sink, lock=True)
        held = row.memory
        if user.scope is not None:
            raise HTTPException(403, f"the agent of run {user.scope.run_id} restores no memory: a writer of it does")
        if row.profile_id is None or access is None:
            raise HTTPException(422, f"memory {memory_id} is no agent's: only an agent's memory is ever archived")
        if not has_role(access.role, "writer"):
            raise HTTPException(403, f"restoring a memory needs the writer role on project {held.project}")
        if held.archived_at is None:
            return held
        await conn.execute(_RESTORED, {"restored_id": memory_id})
        await record(conn, actor_id=user.user_id, token_id=user.token_id, action=RESTORE, target=f"memory:{memory_id}")
        restored = (await memories._load(conn, memory_id)).memory
    log.info("memory restored", extra={"memory_id": memory_id, "project": held.project, "login": user.login})
    return restored
