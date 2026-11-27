"""The index of an agent's memories in the prompt of its runs, and their upkeep (plan agent-profile-workspace,
criterion a3).

The index: the prompt of a run under an agent profile that is not its project's default ends with the memories of that
profile, newest first: each one's name, id, type, description and a stale mark, at most INDEX_BYTES and within the room
the prompt of the run's kind leaves under MAX_PROMPT_BYTES (``with_index``). It lists only what the agent of the run
could open with memory_get: a memory neither deleted nor archived, whose label the run's owner reads through the
agent's sink (``AGENT_SINK``) and whose audience, if it names one, the agent of the run holds as memory_get judges it
(``kg_audience.agent_audience``: for a chat run what both people it is cleared for hold, so a memory a chat run wrote,
which carries ``chat:<id>``, is listed in the next runs of that chat and in runs of people in it). The hub notes the
audiences the index lists as shown to the run (``run_audience``), so what the run writes carries them. A profile
without such a memory, and the default profile, leave the prompt as it was.

The upkeep, the daily job ``hub.memory_upkeep`` (``upkeep``): an agent's memory nobody opened for EXPIRE_AFTER (the
agent of a run opening it with memory_get, ``core.opened``; a new revision counts too) is archived: out of the
index and of the searches and listings of runs (``repository.namespace_condition``), still shown to members, until a
writer of its project restores it (POST /v1/memories/{id}/restore, ``evo-agents hub memory restore``). An agent's
memory that cites code as the Reviewer's evidence does (``code:REPO:PATH[:LINE]``, ``review.parse_evidence``) is stale
while the project's latest knowledge graph has no file at one of those paths, read as the person who owns the memory
(``changes.service.tiers.impact_as_owner``); a project without a graph, or whose graph cannot be read now, keeps the
marks it had. A memory the Curator promoted into a shared one (``promotion``) is out of the index, and the job leaves it
alone.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.memory import AGENT_SINK
from evo_agents.hub.review import EvidenceProblem, parse_evidence
from evo_agents.hub.runs import MAX_PROMPT_BYTES
from evo_agents.hub.server import run_audience
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Forbidden, Unprocessable
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.changes.service.tiers import GraphReader, impact_as_owner
from evo_agents.hub.server.kg_audience import agent_audience
from evo_agents.hub.server.kg_health import owner_access
from evo_agents.hub.server.mcp_text import memory_about
from evo_agents.hub.server.memories import repository
from evo_agents.hub.server.memories.models import INDEX_BYTES, RESTORE, Memory
from evo_agents.hub.server.memories.service import core
from evo_agents.hub.server.memories.service.audience import label_audience
from evo_agents.hub.server.security import Principal, holds
from evo_agents.kg.policy import audience_below

log = logging.getLogger(__name__)

EXPIRE_AFTER = timedelta(days=30)  # an agent's memory nobody opened for this long is archived
CITATION = re.compile(r"\bcode:[A-Za-z0-9][A-Za-z0-9._-]*:[^\s`'\"<>()\[\]{}|,;]+")
TRAILING = ".:"  # punctuation a sentence leaves after a citation


# The index


def _size(lines: list[str]) -> int:
    return len("\n".join(lines).encode()) + 1


async def memory_index(conn: AsyncConnection, run_id: int, room: int = INDEX_BYTES) -> str:
    """The index of the memories of run ``run_id``'s agent profile, within ``room`` and INDEX_BYTES bytes; empty for a
    run under its project's default profile or none, and for a profile with no memory its agent could open."""
    run = await repository.index_run(conn, run_id)
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
    budget, lines, held, shown = min(room, INDEX_BYTES), [], None, set()
    for memory in await repository.indexed(conn, run.profile_id):
        if not access.visible(memory.label, AGENT_SINK):
            continue
        named = label_audience(memory.label)
        if named:  # read once, at the first memory that names an audience, as memory_get does
            held = await agent_audience(conn, run.owner_id, run_id, run.kind) if held is None else held
            if not audience_below(named, held):
                continue
        stale = ", stale" if memory.stale_at is not None else ""
        line = f"- {memory.name} (id {memory.id}, {memory.type}{stale}): {memory_about(memory.body)}"
        if _size([*head, *lines, line]) > budget:
            break
        lines.append(line)
        shown.update(named)
    await run_audience.note(conn, run_id, shown)
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


async def _mark_stale(engine: AsyncEngine, reader: GraphReader, now: datetime) -> dict:
    """The stale marks of the agents' memories, by the graph of each project as each memory's owner reads it."""
    async with UnitOfWork(engine) as conn:
        rows = await repository.citing(conn)
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
            async with UnitOfWork(engine) as conn:
                await repository.mark_stale(conn, marks)
    return counts


async def upkeep(engine: AsyncEngine, blobs, data_dir: Path | None, *, now: datetime | None = None) -> dict:
    """The daily upkeep of the agents' memories: those nobody opened for EXPIRE_AFTER archived, and the stale marks of
    those that cite code set by the projects' graphs (``blobs`` and the cache under ``data_dir``)."""
    now = now or datetime.now(UTC)
    async with UnitOfWork(engine) as conn:
        archived = await repository.archive(conn, now, now - EXPIRE_AFTER)
    counts = {"archived": archived, **await _mark_stale(engine, GraphReader.at(blobs, data_dir), now)}
    log.info("agent memories kept", extra=counts)
    return counts


# Restoring an archived memory


async def restore(engine: AsyncEngine, user: Principal, memory_id: int, sink: str) -> Memory:
    """Bring back an agent's memory the daily upkeep archived: into the index and the searches of its agent's runs
    again, as if just opened, with the audit row ``memory.restore``. A writer of its project who reads it, never the
    agent of a run; a memory not archived is left as it is."""
    async with UnitOfWork(engine) as conn:
        row, access = await core.readable(conn, user, memory_id, sink, lock=True)
        held = row.memory
        if user.scope is not None:
            raise Forbidden(f"the agent of run {user.scope.run_id} restores no memory: a writer of it does")
        if row.profile_id is None or access is None:
            raise Unprocessable(f"memory {memory_id} is no agent's: only an agent's memory is ever archived")
        if not holds(access.role, "writer"):
            raise Forbidden(f"restoring a memory needs the writer role on project {held.project}")
        if held.archived_at is None:
            return held
        await repository.restore(conn, memory_id)
        await Auditor(conn, None, user).record(RESTORE, f"memory:{memory_id}")
        restored = await repository.load(conn, memory_id)
        assert restored is not None
    log.info("memory restored", extra={"memory_id": memory_id, "project": held.project, "login": user.login})
    return restored.memory
