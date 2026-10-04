"""The api's side of a knowledge graph on the hub: the built graphs it keeps in its cache, and the kg_* tools answered
from them with the code of ``evo-agents kg serve``.

A tool call reads the project's latest successful build. Its artifact, the SQLite file the worker uploaded, is
looked up in ``<data dir>/kg/graphs/<project>/<sha256>.sqlite``; when it is not there it is fetched from the blob
store into a temporary file in the same directory, its SHA-256 and size are checked, and only then is it renamed into
place, so a file in the cache always holds the bytes its name promises. A file that does not match is deleted and
never opened. The cache keeps the KEEP files installed last per project: an older one is deleted unless a call is
reading it (an open file stays readable after it is deleted anyway). When the blob store does not answer, the newest
build whose artifact is cached answers instead, and kg_status says which build that is. Files are opened read-only
and immutable: nothing writes them again, so SQLite takes no lock.

``HubSession`` is ``evo_agents.kg.serve.Session`` over such a file, with the hub's read rule as its clearance: the
meet of the caller's grant and the sink's clearance (``ProjectRules.ceiling``). The tools, their schemas, CAP_CHARS
and kg_more are the local server's; the handles kg_more continues live in this process for HANDLE_TTL, per user and
project.

The api runs one process: the in-use counts and the fetch locks are per process.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from evo_agents.hub.blobs import BlobStore, BlobStoreUnavailable, blob_key
from evo_agents.kg.policy import Label, Policy
from evo_agents.kg.serve import HANDLE_TTL, TOOLS, Session, ToolError
from evo_agents.kg.store import Store

TOOL_NAMES = tuple(tool["name"] for tool in TOOLS)
KEEP = 2  # graphs kept per project: the latest, and the one before it to fall back on
MAX_HANDLES = 100  # kg_more handles kept per user and project


class GraphUnavailable(Exception):
    """No graph of the project can be read: the blob store does not answer and none is cached."""


class ArtifactMismatch(Exception):
    """The artifact the blob store returned is not the one the build recorded; it was deleted unread."""


@dataclass(frozen=True)
class BuiltGraph:
    """A successful build, as kg_builds records it."""

    build_id: int
    sha256: str
    size: int
    content_hash: str
    nodes: int
    edges: int
    finished_at: datetime


@dataclass(frozen=True)
class HubProject:
    """What a Session needs to know of its project when the project lives on the hub."""

    name: str


class GraphCache:
    def __init__(self, root: Path, keep: int = KEEP):
        self.root = root
        self.keep = keep
        self._lock = threading.Lock()  # guards the counts and the deletions
        self._fetching: dict[Path, threading.Lock] = {}
        self._in_use: Counter[Path] = Counter()

    def path(self, project: str, sha256: str) -> Path:
        return self.root / project / f"{sha256}.sqlite"

    def _hold(self, path: Path) -> bool:
        with self._lock:
            if not path.exists():
                return False
            self._in_use[path] += 1
            return True

    def release(self, path: Path) -> None:
        with self._lock:
            self._in_use[path] -= 1
            if self._in_use[path] <= 0:
                del self._in_use[path]

    def _fetch(self, store: BlobStore, project: str, graph: BuiltGraph) -> Path:
        """Put ``graph``'s artifact in the cache and hold it. Raises BlobStoreUnavailable, ArtifactMismatch."""
        path = self.path(project, graph.sha256)
        with self._lock:
            fetching = self._fetching.setdefault(path, threading.Lock())
        with fetching:  # two calls wanting the same artifact fetch it once
            try:
                if self._hold(path):
                    return path
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
                try:
                    found = store.fetch(blob_key(graph.sha256), tmp, graph.size)
                    if found is None:
                        raise ArtifactMismatch(
                            f"the graph of build {graph.build_id} of project {project} is missing from the blob store"
                        )
                    if found != (graph.sha256, graph.size):
                        raise ArtifactMismatch(
                            f"the graph of build {graph.build_id} of project {project} that the blob store returned "
                            "does not have the SHA-256 the build recorded; it was not opened"
                        )
                    os.replace(tmp, path)
                    os.utime(path)
                finally:
                    tmp.unlink(missing_ok=True)
                with self._lock:
                    self._in_use[path] += 1
            finally:
                with self._lock:
                    self._fetching.pop(path, None)
        self.evict(project)
        return path

    def evict(self, project: str) -> list[Path]:
        """Delete the graphs of ``project`` beyond the KEEP installed last, except those a call is reading."""
        removed = []
        with self._lock:
            directory = self.root / project
            files = sorted(directory.glob("*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
            for path in files[self.keep :]:
                if self._in_use[path]:
                    continue
                path.unlink(missing_ok=True)
                removed.append(path)
        return removed

    def acquire(self, store: BlobStore | None, project: str, graphs: list[BuiltGraph]) -> tuple[Path, BuiltGraph]:
        """The cached file of the newest of ``graphs`` (successful builds, newest first), held until ``release``;
        when the blob store does not answer, the newest one cached. Raises GraphUnavailable, ArtifactMismatch."""
        latest = graphs[0]
        if self._hold(self.path(project, latest.sha256)):
            return self.path(project, latest.sha256), latest
        if store is not None:
            try:
                return self._fetch(store, project, latest), latest
            except BlobStoreUnavailable:
                pass
        for older in graphs[1:]:
            if self._hold(self.path(project, older.sha256)):
                return self.path(project, older.sha256), older
        raise GraphUnavailable(
            f"the blob store did not answer and no graph of project {project} is cached on this hub; try again shortly"
        )


class HubSession(Session):
    """The tools of ``kg serve`` over a graph the hub built. ``ceiling`` is the highest label the caller may read
    (None: nothing)."""

    def __init__(
        self,
        project: str,
        policy: Policy,
        ceiling: Label | None,
        sink: str,
        store: Store,
        graph: BuiltGraph,
        latest: BuiltGraph,
    ):
        super().__init__(None, sink)
        self.project = HubProject(project)
        self.policy = policy
        self.graph_store = store
        self.graph = graph
        self.latest = latest
        if ceiling is None:
            self.error = (
                f"nothing of project {project} is visible through sink {sink!r} with your grant: the sink is not "
                "declared in its knowledge.yaml (or clears a level its ladder lacks)"
            )
        else:
            self.clearance = (ceiling.level, ceiling.location)

    def _store(self) -> tuple[Store, int]:
        b = self.graph_store.latest_ready()
        if b is None:
            raise ToolError(f"build {self.graph.build_id} of project {self.project.name} holds no ready graph")
        return self.graph_store, b

    def tool_kg_status(self) -> tuple[str, dict]:
        clr = (self.policy.level_name(self.clearance[0]), self.policy.location_name(self.clearance[1]))
        summary = self.graph_store.summary()
        coverage = summary.get("coverage") or {}
        hub_build = {
            "build_id": self.graph.build_id,
            "artifact_sha256": self.graph.sha256,
            "content_hash": self.graph.content_hash,
            "finished_at": self.graph.finished_at.isoformat(),
            "latest": self.graph.build_id == self.latest.build_id,
        }
        lines = [
            f"sink {self.sink}: clearance {clr[0]}/{clr[1]}",
            f"project {self.project.name} on the hub: build {self.graph.build_id} finished "
            f"{hub_build['finished_at']}, {summary.get('nodes', 0)} nodes, {summary.get('edges', 0)} edges, "
            f"{summary.get('units', 0)} units, content {self.graph.content_hash}",
        ]
        if not hub_build["latest"]:
            lines.append(
                f"note: build {self.latest.build_id} is newer, but the blob store did not answer; this is the newest "
                "graph cached here"
            )
        for src in coverage.get("sources", []):
            lines.append(
                f"  {src['id']:<22} {src['items']:>6} items, mentions {src['resolved']}/{src['mentions']}"
                f" ({src['resolved_ratio']:.0%}), {src['dangling']} dangling"
            )
        lines += [f"warning: {w}" for w in coverage.get("warnings", [])]
        status = {
            "project": self.project.name,
            "backend": "hub",
            "ok": bool(summary.get("ready")),
            "hub_build": hub_build,
            "graph": summary,
            "warnings": coverage.get("warnings", []),
            "sink": {"id": self.sink, "clearance": {"level": clr[0], "location": clr[1]}},
            "summary": self.project.name,
        }
        return "\n".join(lines), status


class Handles:
    """The kg_more handles of every user and project, shared by the calls of this process."""

    def __init__(self, ttl: float = HANDLE_TTL, limit: int = MAX_HANDLES):
        self.ttl = ttl
        self.limit = limit
        self._lock = threading.Lock()
        self._owned: dict[tuple, dict[str, tuple[float, deque]]] = {}

    def call(self, session: Session, owner: tuple, name: str, arguments: dict) -> dict:
        handle = arguments.get("handle") if name == "kg_more" else None
        with self._lock:
            mine = self._owned.get(owner, {})
            now = time.monotonic()
            for key in [key for key, (made, _) in mine.items() if now - made > self.ttl]:
                del mine[key]
            if isinstance(handle, str) and handle in mine:
                session.handles[handle] = mine[handle]
        result = session.call(name, arguments)
        with self._lock:
            mine = self._owned.setdefault(owner, {})
            if isinstance(handle, str) and handle not in session.handles:
                mine.pop(handle, None)  # read to the end
            mine.update(session.handles)
            for key in sorted(mine, key=lambda k: mine[k][0])[: max(len(mine) - self.limit, 0)]:
                del mine[key]
            if not mine:
                del self._owned[owner]
        return result


def answer(
    cache: GraphCache,
    store: BlobStore | None,
    handles: Handles,
    owner: tuple,
    project: str,
    policy: Policy,
    ceiling: Label | None,
    sink: str,
    graphs: list[BuiltGraph],
    tool: str,
    arguments: dict,
) -> dict:
    """One kg_* call on the newest graph of ``project`` that can be read. Blocking: run it in a thread, which also
    keeps the SQLite connection in the thread that made it."""
    path, graph = cache.acquire(store, project, graphs)
    try:
        opened = Store.open_file(path, immutable=True)
        try:
            session = HubSession(project, policy, ceiling, sink, opened, graph, graphs[0])
            return handles.call(session, owner, tool, arguments)
        finally:
            opened.close()
    finally:
        cache.release(path)
