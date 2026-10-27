"""The api's side of a knowledge graph on the hub: the built graphs it keeps in its cache, and the kg_* tools answered
from them with the code of ``evo-agents kg serve``.

A tool call reads the project's latest successful build that still holds an artifact (``BuiltGraph``, which
``built_graphs`` of ``evo_agents.hub.server.kg`` reads from Postgres: this module imports no database library). Its
artifact, the SQLite file the worker uploaded, is looked up in ``<data dir>/kg/graphs/<project>/<sha256>.sqlite``;
when it is not there it is fetched from the blob store into a temporary file in the same directory, its SHA-256 and
size are checked, and only then is it renamed into place, so a file in the cache always holds the bytes its name
promises. A file that does not match is deleted and never opened. The cache keeps the KEEP files installed last per
project: an older one is deleted unless a call is reading it (an open file stays readable after it is deleted anyway).
When the blob store does not answer, the newest build whose artifact is cached answers instead, and kg_status says
which build that is, and the commit each git source of it read, with how far behind its remote's default branch the
machine that read it counted (the hub has no checkout to count again). Files are opened read-only and immutable: nothing writes them again, so SQLite takes no lock.

``HubSession`` is ``evo_agents.kg.serve.Session`` over such a file, with the hub's read rule as its clearance: the
meet of the caller's grant and the sink's clearance (``ProjectRules.ceiling``), whose audience is the caller's, as the
hub computed it for this call (``evo_agents.hub.server.kg_audience``). The tools, their schemas, CAP_CHARS and kg_more
are the local server's; the handles kg_more continues live in this process for HANDLE_TTL, per user and project, and
continue only for a caller whose audience still holds what the result revealed.

kg_status names the build's totals and the coverage of each source when the caller's audience holds every audience of
the build. Otherwise it counts what that audience holds (``Store.seen``); of a source with items that have an audience
it says only how many of them the caller sees; and it leaves out what the build says of all items at once: the samples,
issues and errors of its coverage, its size and its audiences. The web's graph pages count the same way
(``evo_agents.hub.kg_web``).

``unknown`` looks node ids and paths up in the same graph, as a session of the caller reads it, for a check that only
needs to know which of them it lacks: the sources of a finding of the core plugin srs
(``evo_agents.hub.server.kg.unknown_refs``). An id names a node as kg_node finds it; a path, ``repo:path`` or a path
alone, names one when a visible File or Document node carries exactly that path. It also gives the author emails of
the LATEST_COMMITS newest commits visible to the session that changed each file or document the ids and paths name
(``changes`` edges, newest by the committer time the Commit node keeps), each address once: the people a settle chat
of srs invites for that source.

The api runs one process: the in-use counts and the fetch locks are per process.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from evo_agents.hub.blobs import BlobStore, BlobStoreUnavailable, blob_key
from evo_agents.hub.srs_baseline import CODE_COMMENT, COMMIT, PLAN_COMPLETED, PULL_REQUEST, STEP_DONE, Evidence
from evo_agents.kg import freshness
from evo_agents.kg import store as kg_store
from evo_agents.kg.policy import Label, Policy, audience_below
from evo_agents.kg.serve import HANDLE_TTL, IMPACT_STATUSES, TOOLS, Session, ToolError
from evo_agents.kg.store import StaleSchema, Store

TOOL_NAMES = tuple(tool["name"] for tool in TOOLS)
KEEP = 2  # graphs kept per project: the latest, and the one before it to fall back on
MAX_HANDLES = 100  # kg_more handles kept per user and project
LATEST_COMMITS = 3  # the newest commits of a file whose authors ``unknown`` gives
COMMITS_PAGE = 500  # edges into a file read at once to find its commits
MAX_COMMITS_READ = 5_000  # edges into one file read at most; a file changed more often is read that far
MAX_MENTIONS_READ = 500  # edges into the node of one use case read at most


class GraphUnavailable(Exception):
    """No graph of the project can be read: the blob store does not answer and none is cached."""


class GraphOutdated(GraphUnavailable):
    """The newest graph was written with an older store schema than this hub reads: a new build will replace it."""

    def __init__(self, project: str, graph: BuiltGraph, exc: StaleSchema):
        super().__init__(
            f"the graph of project {project} (build {graph.build_id}) was written with store schema {exc.found}, and "
            f"this hub reads {kg_store.SCHEMA_VERSION}: a new build of it is queued; try again once it has run"
        )


def open_graph(path: Path, project: str, graph: BuiltGraph) -> Store:
    """The cached artifact of ``graph`` as a read-only store; GraphOutdated when an older schema wrote it."""
    try:
        return Store.open_file(path, immutable=True)
    except StaleSchema as exc:
        raise GraphOutdated(project, graph, exc) from None


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
                    with open(tmp, "wb") as handle:
                        found = store.fetch(blob_key(graph.sha256), handle, graph.size)
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

    def drop(self, project: str, sha256: str) -> bool:
        """Delete the cached file of artifact ``sha256`` of ``project``, once the hub deleted the artifact; whether
        there was one. A call reading it reads on: an open file stays readable after it is deleted."""
        with self._lock:
            path = self.path(project, sha256)
            found = path.exists()
            path.unlink(missing_ok=True)
        return found

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
            self.audience = ceiling.audience

    def _graph(self) -> tuple[Store, int]:
        b = self.graph_store.latest_ready()
        if b is None:
            raise ToolError(f"build {self.graph.build_id} of project {self.project.name} holds no ready graph")
        return self.graph_store, b

    def whole(self, b: int) -> bool:
        """Whether the session's audience holds every audience of build ``b``."""
        return audience_below(self.graph_store.top_audience(b), self.audience)

    def within(self, summary: dict) -> dict:
        """The build's ``summary`` (``Store.summary``) as the session's audience may read it (see the module)."""
        b = summary.get("build_id")
        if not summary.get("ready") or self.whole(b):
            return summary
        self.graph_store.reader = self.audience
        seen = self.graph_store.seen(b)
        coverage = summary.get("coverage") or {}
        sources = [
            {"id": s["id"], "items": seen["items"].get(s["id"], 0)} if s["id"] in seen["audience"] else s
            for s in coverage.get("sources", [])
        ]
        stats = {k: v for k, v in (summary.get("stats") or {}).items() if k not in ("size", "top_audience")}
        kept = {k: summary[k] for k in ("ready", "build_id", "built_at", "content_hash")}
        counts = {k: seen[k] for k in ("nodes", "edges", "units")}
        coverage = {"sources": sources, "warnings": coverage.get("warnings", [])}
        return {**kept, **counts, "coverage": coverage, "stats": stats}

    def counted(self, graph: BuiltGraph) -> BuiltGraph:
        """``graph`` with the nodes and edges the session's audience holds, when it does not hold every audience."""
        b = self.graph_store.latest_ready()
        if b is None or self.whole(b):
            return graph
        self.graph_store.reader = self.audience
        seen = self.graph_store.seen(b)
        return replace(graph, nodes=seen["nodes"], edges=seen["edges"])

    def tool_kg_status(self) -> tuple[str, dict]:
        clr = (self.policy.level_name(self.clearance[0]), self.policy.location_name(self.clearance[1]))
        summary = self.within(self.graph_store.summary())
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
            if "mentions" not in src:  # items with an audience (``within``)
                lines.append(f"  {src['id']:<22} {src['items']:>6} items you can read")
                continue
            read = f", read {freshness.describe(src['read'])}" if isinstance(src.get("read"), dict) else ""
            lines.append(
                f"  {src['id']:<22} {src['items']:>6} items, mentions {src['resolved']}/{src['mentions']}"
                f" ({src['resolved_ratio']:.0%}), {src['dangling']} dangling{read}"
            )
        lines += [f"warning: {w}" for w in coverage.get("warnings", [])]
        status = {
            "project": self.project.name,
            "backend": "hub",
            "ok": bool(summary.get("ready")),
            "hub_build": hub_build,
            "graph": summary,
            "warnings": coverage.get("warnings", []),
            "reads": freshness.reads(coverage),
            "sink": {"id": self.sink, "clearance": {"level": clr[0], "location": clr[1]}},
            "summary": self.project.name,
        }
        return "\n".join(lines), status

    def unknown(self, ids: list[str], paths: list[str]) -> tuple[list[str], list[str], list[str]]:
        """(of node ``ids`` and ``paths``, those no node visible to the session has; the author emails of the newest
        commits of the files and documents they name), as the module says; ToolError when nothing of the project is
        visible to the session, or the graph holds no ready build."""
        if self.error:
            raise ToolError(self.error)
        store, b = self._store()
        lacking, named = [], set()
        for node_id in ids:
            found = self._lookup(store, node_id, b)
            if found is None:
                lacking.append(node_id)
            elif found["kind"] in ("File", "Document"):
                named.add(found["id"])
        files = [found for found in store.path_nodes(b) if self.visible(found["label"])] if paths else []
        held: dict[tuple, set[str]] = {}
        for found in files:
            held.setdefault((found["props"].get("repo"), found["props"]["path"]), set()).add(found["id"])
        bare: dict[str, set[str]] = {}
        for (_, path), nodes in held.items():
            bare.setdefault(path, set()).update(nodes)
        missing = []
        for path in paths:
            repo, sep, rest = path.partition(":")
            if sep and rest and (repo, rest) in held:
                named |= held[(repo, rest)]
            elif path in bare:
                named |= bare[path]
            else:
                missing.append(path)
        return lacking, missing, self._authors(store, b, sorted(named))

    def mentions(self, codes: list[str]) -> dict[str, list[Evidence]]:
        """For each use case code of ``codes`` (the node ``usecase:<code>``), what the graph shows of its delivery as
        the session reads it: a ``mentions`` edge from a plan whose steps are all done, a step done, a commit, a merged
        pull request or a source file (a comment of the code). ToolError when nothing of the project is visible or the
        graph holds no ready build."""
        if self.error:
            raise ToolError(self.error)
        store, b = self._store()
        found: dict[str, list[Evidence]] = {}
        for code in codes:
            edges = store.edges_into(
                f"usecase:{code}", b, ("mentions",), IMPACT_STATUSES, self.clearance, limit=MAX_MENTIONS_READ
            )
            sources = store.nodes([edge["src"] for edge in edges if self.visible(edge["label"])], b)
            found[code] = sorted(
                {
                    item
                    for node in sources.values()
                    if self.visible(node["label"]) and (item := _evidence(node)) is not None
                },
                key=lambda item: (item.kind, item.ref),
            )
        return found

    def _authors(self, store: Store, b: int, file_ids: list[str]) -> list[str]:
        """The author emails of the LATEST_COMMITS newest commits visible to the session that changed each of
        ``file_ids``, each address once, in the order found."""
        found: list[str] = []
        for file_id in file_ids:
            edges: list[dict] = []
            while len(edges) < MAX_COMMITS_READ:
                page = store.edges_into(
                    file_id, b, ("changes",), IMPACT_STATUSES, self.clearance, limit=COMMITS_PAGE, offset=len(edges)
                )
                edges += page
                if len(page) < COMMITS_PAGE:
                    break
            sources = [edge["src"] for edge in edges if self.visible(edge["label"])]
            commits = [
                node
                for node in store.nodes(sources, b).values()
                if node["kind"] == "Commit" and self.visible(node["label"])
            ]
            commits.sort(key=lambda node: (str(node["props"].get("time") or ""), node["id"]), reverse=True)
            for node in commits[:LATEST_COMMITS]:
                email = node["props"].get("author_email")
                if isinstance(email, str) and email and email not in found:
                    found.append(email)
        return found


def _evidence(node: dict) -> Evidence | None:
    """The evidence of delivery a node that mentions a use case is, or None when it is none (a plan with steps left, a
    step not done, a pull request not merged, a document)."""
    props, kind = node["props"], node["kind"]
    if kind == "Plan" and props.get("steps") and props.get("done") == props.get("steps"):
        found = PLAN_COMPLETED
    elif kind == "PlanStep" and props.get("status") == "done":
        found = STEP_DONE
    elif kind == "Commit":
        found = COMMIT
    elif kind == "PullRequest" and props.get("status") == "merged":
        found = PULL_REQUEST
    elif kind == "File":
        found = CODE_COMMENT
    else:
        return None
    return Evidence(found, node["id"], str(node.get("name") or "")[:120])


class Handles:
    """The kg_more handles of every user and project, shared by the calls of this process."""

    def __init__(self, ttl: float = HANDLE_TTL, limit: int = MAX_HANDLES):
        self.ttl = ttl
        self.limit = limit
        self._lock = threading.Lock()
        self._owned: dict[tuple, dict[str, tuple]] = {}  # owner: handle: as ``Session.handles`` keeps it

    def call(self, session: Session, owner: tuple, name: str, arguments: dict) -> dict:
        handle = arguments.get("handle") if name == "kg_more" else None
        with self._lock:
            mine = self._owned.get(owner, {})
            now = time.monotonic()
            for key in [key for key, (made, *_) in mine.items() if now - made > self.ttl]:
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
        opened = open_graph(path, project, graph)
        try:
            session = HubSession(project, policy, ceiling, sink, opened, graph, graphs[0])
            return handles.call(session, owner, tool, arguments)
        finally:
            opened.close()
    finally:
        cache.release(path)


def mentioning(
    cache: GraphCache,
    store: BlobStore | None,
    project: str,
    policy: Policy,
    ceiling: Label | None,
    sink: str,
    graphs: list[BuiltGraph],
    codes: list[str],
) -> dict[str, list[Evidence]]:
    """What the newest graph of ``project`` that can be read shows of the delivery of each use case code of ``codes``
    (``HubSession.mentions``), read through ``sink`` under ``ceiling``. Blocking, as ``answer``. Raises
    GraphUnavailable, ArtifactMismatch; ToolError when nothing of the project is visible."""
    path, graph = cache.acquire(store, project, graphs)
    try:
        opened = open_graph(path, project, graph)
        try:
            return HubSession(project, policy, ceiling, sink, opened, graph, graphs[0]).mentions(codes)
        finally:
            opened.close()
    finally:
        cache.release(path)


@dataclass(frozen=True)
class UnknownRefs:
    """Of the node ids and paths looked up in a project's graph, those it lacks; ``why`` when they could not be looked
    up at all (no graph, none readable now, nothing of the project visible), and then each counts as lacking.
    ``authors``: the author emails of the newest commits of the files and documents found (``HubSession.unknown``)."""

    ids: tuple[str, ...]
    paths: tuple[str, ...]
    why: str | None = None
    authors: tuple[str, ...] = ()


def unknown(
    cache: GraphCache,
    store: BlobStore | None,
    project: str,
    policy: Policy,
    ceiling: Label | None,
    sink: str,
    graphs: list[BuiltGraph],
    ids: list[str],
    paths: list[str],
) -> UnknownRefs:
    """Which of ``ids`` and ``paths`` the newest graph of ``project`` that can be read lacks, read through ``sink``
    under ``ceiling``, with the authors of the newest commits of the files they name. Blocking, as ``answer``. Raises
    GraphUnavailable, ArtifactMismatch."""
    path, graph = cache.acquire(store, project, graphs)
    try:
        opened = open_graph(path, project, graph)
        try:
            session = HubSession(project, policy, ceiling, sink, opened, graph, graphs[0])
            lacking, missing, authors = session.unknown(ids, paths)
        except ToolError as exc:
            return UnknownRefs(tuple(ids), tuple(paths), str(exc))
        finally:
            opened.close()
    finally:
        cache.release(path)
    return UnknownRefs(tuple(lacking), tuple(missing), None, tuple(authors))
