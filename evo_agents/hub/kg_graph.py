"""The api's side of a knowledge graph on the hub: the kg_* tools answered with the code of ``evo-agents kg serve``
from the snapshot of a project's graph that the hub holds in memory, made of its tables
(``evo_agents.hub.kg_snapshots``; ``reading``).

A tool call reads the project's latest successful build (``BuiltGraph``, which ``built_graphs`` of
``evo_agents.hub.server.kg`` reads from Postgres: this module imports no database library). When the snapshot held is
of an older build than the newest, that one answers, and kg_status says which build that is, and the commit each git
source of it read, with how far behind its remote's default branch the machine that read it counted (the hub has no
checkout to count again).

``HubSession`` is ``evo_agents.kg.serve.Session`` over such a snapshot, with the hub's read rule as its clearance: the
meet of the caller's grant and the sink's clearance (``ProjectRules.ceiling``), whose audience is the caller's, as the
hub computed it for this call (``evo_agents.hub.server.kg_audience``). The tools, their schemas, CAP_CHARS and kg_more
are the local server's; the handles kg_more continues live in this process for HANDLE_TTL, per user and project, and
continue only for a caller whose audience still holds what the result revealed.

kg_status names the build's totals and the coverage of each source when the caller's audience holds every audience of
the build: per source its mentions, the commit it read and its code backend, then the clusters and the codes defined in
two or more places (``evo_agents.kg.status``). Otherwise it counts what that audience holds (``SnapshotView.seen``); of
a source with items that have an audience it says only how many of them the caller sees; and it leaves out what the
build says of all items at once: the samples, issues and errors of its coverage, its size and its audiences; of its
vectors it keeps the mode, the model and the error, not their counts. The vectors are the hub's record of the build
(``kg_builds.coverage``): the graph file's own coverage is written before the build embeds. The web's graph pages count
the same way (``evo_agents.hub.kg_web``). A source knowledge.yaml labels above the sink's clearance shows its id alone,
and a build whose highest label is above it shows its counts alone (``HubSession.cleared``); what it shows raises the
session's label, as the other tools' results do.

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

import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime

from evo_agents.hub.srs_baseline import CODE_COMMENT, COMMIT, PLAN_COMPLETED, PULL_REQUEST, STEP_DONE, Evidence
from evo_agents.kg import freshness
from evo_agents.kg import settings as kg_settings
from evo_agents.kg import status as status_text
from evo_agents.kg.explore import Explorer
from evo_agents.kg.policy import Label, Policy, audience_below
from evo_agents.kg.serve import HANDLE_TTL, TOOLS, Session
from evo_agents.kg.serve_impact import IMPACT_STATUSES
from evo_agents.kg.serve_view import ANSWERS_NOTHING, ToolError
from evo_agents.kg.snapshot import SnapshotView

TOOL_NAMES = tuple(tool["name"] for tool in TOOLS)
MAX_HANDLES = 100  # kg_more handles kept per user and project
LATEST_COMMITS = 3  # the newest commits of a file whose authors ``unknown`` gives
COMMITS_PAGE = 500  # edges into a file read at once to find its commits
MAX_COMMITS_READ = 5_000  # edges into one file read at most; a file changed more often is read that far
MAX_MENTIONS_READ = 500  # edges into the node of one use case read at most


class GraphUnavailable(Exception):
    """No graph of the project can be read: none of its builds is held as a snapshot of the hub's tables yet."""


@dataclass(frozen=True)
class BuiltGraph:
    """A successful build, as kg_builds records it."""

    build_id: int
    sha256: str | None  # the graph file an older build uploaded; None since the hub keeps the graph in its tables
    size: int | None
    content_hash: str
    nodes: int
    edges: int
    finished_at: datetime
    vectors: dict | None = field(default=None, compare=False)  # kg_builds.coverage's vectors (``semantic.Indexed``)


@dataclass(frozen=True)
class HubProject:
    """What a Session needs to know of its project when the project lives on the hub."""

    name: str


class HubSession(Session):
    """The tools of ``kg serve`` over a graph the hub built. ``ceiling`` is the highest label the caller may read
    (None: nothing)."""

    def __init__(
        self,
        project: str,
        policy: Policy,
        ceiling: Label | None,
        sink: str,
        store: SnapshotView,
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

    def _graph(self) -> tuple[SnapshotView, int]:
        b = self.graph_store.latest_ready()
        if b is None:
            raise ToolError(f"build {self.graph.build_id} of project {self.project.name} holds no ready graph")
        return self.graph_store, b

    def whole(self, b: int) -> bool:
        """Whether the session's audience holds every audience of build ``b``."""
        return audience_below(self.graph_store.top_audience(b), self.audience)

    def recorded(self, summary: dict) -> dict:
        """The graph file's ``summary`` with the vectors the hub recorded of the build, which the file lacks."""
        if not summary.get("ready") or not isinstance(self.graph.vectors, dict):
            return summary
        return {**summary, "coverage": {**(summary.get("coverage") or {}), "vectors": self.graph.vectors}}

    def within(self, summary: dict) -> dict:
        """The build's ``summary`` as the session's audience may read it (see the module)."""
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
        vectors = coverage.get("vectors")
        coverage = {"sources": sources, "warnings": coverage.get("warnings", [])}
        if isinstance(vectors, dict):  # whether the build embedded and with what model, not how many rows
            coverage["vectors"] = {k: vectors[k] for k in ("mode", "model", "error") if k in vectors}
        return {**kept, **counts, "coverage": coverage, "stats": stats}

    def counted(self, graph: BuiltGraph) -> BuiltGraph:
        """``graph`` with the nodes and edges the session's audience holds, when it does not hold every audience."""
        b = self.graph_store.latest_ready()
        if b is None or self.whole(b):
            return graph
        self.graph_store.reader = self.audience
        seen = self.graph_store.seen(b)
        return replace(graph, nodes=seen["nodes"], edges=seen["edges"])

    def cleared(self, summary: dict) -> dict:
        """``summary`` as the sink's clearance reads it, the session's label raised by what it shows: a source
        knowledge.yaml labels above the clearance shows its id alone; a build whose highest label is above it shows
        its counts, not its coverage or stats, which name items."""
        if not summary.get("ready"):
            return summary
        assert self.clearance is not None, ANSWERS_NOTHING
        b = int(summary["build_id"])
        level, location = self.graph_store.top_label(b)
        if level > self.clearance[0] or location > self.clearance[1]:
            return {k: v for k, v in summary.items() if k not in ("coverage", "stats")}
        coverage = summary.get("coverage") or {}
        sources = []
        for src in coverage.get("sources", []):
            if self.policy.declares(src["id"]):
                label = self.policy.source_label(src["id"])
                shown = (label.level, label.location, label.integrity)
                if not self.visible(shown):
                    sources.append({"id": src["id"], "withheld": True})
                    continue
                self._saw(shown)
            sources.append(src)
        whole = self.whole(b)
        self._saw((level, location, "U", self.graph_store.top_audience(b) if whole else frozenset()))
        return {**summary, "coverage": {**coverage, "sources": sources}}

    def tool_kg_status(self) -> tuple[str, dict]:
        clr = (self.policy.level_name(self.clearance[0]), self.policy.location_name(self.clearance[1]))
        summary = self.cleared(self.within(self.recorded(self.graph_store.summary())))
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
            behind = "its graph is not loaded yet; this is the newest graph loaded"
            lines.append(f"note: build {self.latest.build_id} is newer, but {behind}")
        for src in coverage.get("sources", []):
            if src.get("withheld"):  # above the clearance of the sink (``cleared``)
                lines.append(f"  {src['id']:<22} withheld: above the clearance of this sink")
                continue
            if "mentions" not in src:  # items with an audience (``within``)
                lines.append(f"  {src['id']:<22} {src['items']:>6} items you can read")
                continue
            read = f", read {freshness.describe(src['read'])}" if isinstance(src.get("read"), dict) else ""
            lines.append(
                f"  {src['id']:<22} {src['items']:>6} items, mentions {src['resolved']}/{src['mentions']}"
                f" ({src['resolved_ratio']:.0%}), {src['dangling']} dangling{read}"
            )
            if src.get("code"):
                lines.append(f"    {status_text.code_line(src['code'])}")
        if "clusters" in coverage:
            lines.append(status_text.cluster_line(coverage["clusters"]))
        multiple = coverage.get("multiple_definitions")
        if multiple is not None:  # a convention warning, not a failure: ``ok`` stays as it is
            multiple = {**multiple, "hint": status_text.DERIVED_FROM_HINT if multiple["codes"] else None}
            lines += status_text.multiple_lines(multiple)
        lines += [f"warning: {w}" for w in coverage.get("warnings", [])]
        status = {
            "project": self.project.name,
            "backend": "hub",
            "ok": bool(summary.get("ready")),
            "hub_build": hub_build,
            "graph": summary,
            "warnings": coverage.get("warnings", []),
            "multiple_definitions": multiple,
            "reads": freshness.reads(coverage),
            "sink": {"id": self.sink, "clearance": {"level": clr[0], "location": clr[1]}},
            "summary": self.project.name,
        }
        return "\n".join(lines), status

    def node_kinds(self, ids: list[str]) -> dict[str, tuple[str, str]]:
        """Of node ``ids``, those a node visible to the session has: each to (its stored id, its kind). A hidden node
        is left out as a missing one is (``_lookup``). ToolError as ``unknown``."""
        if self.error:
            raise ToolError(self.error)
        store, b = self._store()
        found = {}
        for node_id in dict.fromkeys(ids):
            n = self._lookup(store, node_id, b)
            if n is not None:
                found[node_id] = (n["id"], n["kind"])
        return found

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

    def _authors(self, store: SnapshotView, b: int, file_ids: list[str]) -> list[str]:
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
        # owner: (made, nodes kg_context gave, times): the session discount of ``evo_agents.kg.context_pack``
        self._given: dict[tuple, tuple[float, Counter]] = {}

    def call(self, session: Session, owner: tuple, name: str, arguments: dict) -> dict:
        handle = arguments.get("handle") if name == "kg_more" else None
        with self._lock:
            mine = self._owned.get(owner, {})
            now = time.monotonic()
            for key in [key for key, (made, *_) in mine.items() if now - made > self.ttl]:
                del mine[key]
            if isinstance(handle, str) and handle in mine:
                session.handles[handle] = mine[handle]
            for key in [key for key, (made, _) in self._given.items() if now - made > self.ttl]:
                del self._given[key]
            session.given = Counter(self._given.get(owner, (0.0, Counter()))[1])
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
            if session.given and (owner in self._given or len(self._given) < self.limit):
                self._given[owner] = (time.monotonic(), session.given)
        return result


class WholeSession(HubSession):
    """A HubSession whose answer is the tool's data whole, never cut for kg_more, and no text: what a read across
    projects counts before it reads again within the clearance it may show (``server.agent_reads``)."""

    def _envelope(self, text: str, data: dict) -> dict:
        return {"structuredContent": data}


Snapshotted = tuple[SnapshotView, BuiltGraph]  # a reader's view of a snapshot, and the build it was made of


@contextmanager
def reading(project: str, snapshot: Snapshotted | None) -> Iterator[Snapshotted]:
    """(the graph to read, the build it is): ``snapshot``, a view of the snapshot of one of the project's graphs made
    of the hub's tables (``evo_agents.hub.kg_snapshots``). Raises GraphUnavailable when there is none."""
    if snapshot is None:
        raise GraphUnavailable(f"no graph of project {project} is held on this hub yet; try again shortly")
    yield snapshot


def answer(
    handles: Handles,
    owner: tuple,
    project: str,
    policy: Policy,
    ceiling: Label | None,
    sink: str,
    graphs: list[BuiltGraph],
    tool: str,
    arguments: dict,
    explore: bool = False,
    tuning: kg_settings.Tuning | None = None,
    whole: bool = False,
    named_graphs: dict[str, frozenset[str]] | None = None,
    snapshot: Snapshotted | None = None,
) -> dict:
    """One kg_* call on the newest graph of ``project`` that can be read; ``explore``: a kg_search of an agent's, which
    may explore the ranking (``evo_agents.kg.explore``). Blocking: run it in a thread. ``tuning`` is what the
    project's search is tuned by (its kg_settings); ``whole``: the data alone, uncut (``WholeSession``);
    ``named_graphs``: the named graph the call's ``graph`` names, to its sources, as the database holds it
    (``evo_agents.kg.graph_scope``); ``snapshot``: as ``reading``."""
    with reading(project, snapshot) as (opened, graph):
        if tuning is not None:
            opened.tuning = tuning
        session = (WholeSession if whole else HubSession)(project, policy, ceiling, sink, opened, graph, graphs[0])
        session.explorer = Explorer() if explore else None
        session.named_graphs = dict(named_graphs or {})
        return handles.call(session, owner, tool, arguments)


def mentioning(
    project: str,
    policy: Policy,
    ceiling: Label | None,
    sink: str,
    graphs: list[BuiltGraph],
    codes: list[str],
    snapshot: Snapshotted | None = None,
) -> dict[str, list[Evidence]]:
    """What the newest graph of ``project`` that can be read shows of the delivery of each use case code of ``codes``
    (``HubSession.mentions``), read through ``sink`` under ``ceiling``. Blocking, as ``answer``. Raises
    GraphUnavailable; ToolError when nothing of the project is visible."""
    with reading(project, snapshot) as (opened, graph):
        return HubSession(project, policy, ceiling, sink, opened, graph, graphs[0]).mentions(codes)


@dataclass(frozen=True)
class UnknownRefs:
    """Of the node ids and paths looked up in a project's graph, those it lacks; ``why`` when they could not be looked
    up at all (no graph, none readable now, nothing of the project visible), and then each counts as lacking.
    ``authors``: the author emails of the newest commits of the files and documents found (``HubSession.unknown``)."""

    ids: tuple[str, ...]
    paths: tuple[str, ...]
    why: str | None = None
    authors: tuple[str, ...] = ()


def node_kinds(
    project: str,
    policy: Policy,
    ceiling: Label | None,
    sink: str,
    graphs: list[BuiltGraph],
    ids: list[str],
    snapshot: Snapshotted | None = None,
) -> dict[str, tuple[str, str]]:
    """``HubSession.node_kinds`` of ``ids`` in the newest graph of ``project`` that can be read, through ``sink`` under
    ``ceiling``: what an entity map may cite (``evo_agents.kg.entity_map.check``). Blocking. Raises ToolError,
    GraphUnavailable."""
    with reading(project, snapshot) as (opened, graph):
        return HubSession(project, policy, ceiling, sink, opened, graph, graphs[0]).node_kinds(ids)


def read_labelled(
    project: str,
    ceiling: Label | None,
    graphs: list[BuiltGraph],
    read,
    snapshot: Snapshotted | None = None,
):
    """``read(graph_store, clearance)`` on the newest graph of ``project`` that can be read, its reader the audience of
    ``ceiling`` and ``clearance`` its (level, location), None when ``ceiling`` is: for a caller that filters the rows
    itself (``evo_agents.hub.kg_entity_map_run``); ``snapshot``: as ``reading``. Blocking. Raises GraphUnavailable."""
    with reading(project, snapshot) as (opened, _):
        if ceiling is not None:
            opened.reader = ceiling.audience
        return read(opened, None if ceiling is None else (ceiling.level, ceiling.location))


def unknown(
    project: str,
    policy: Policy,
    ceiling: Label | None,
    sink: str,
    graphs: list[BuiltGraph],
    ids: list[str],
    paths: list[str],
    snapshot: Snapshotted | None = None,
) -> UnknownRefs:
    """Which of ``ids`` and ``paths`` the newest graph of ``project`` that can be read lacks, read through ``sink``
    under ``ceiling``, with the authors of the newest commits of the files they name. Blocking, as ``answer``. Raises
    GraphUnavailable."""
    with reading(project, snapshot) as (opened, graph):
        try:
            session = HubSession(project, policy, ceiling, sink, opened, graph, graphs[0])
            lacking, missing, authors = session.unknown(ids, paths)
        except ToolError as exc:
            return UnknownRefs(tuple(ids), tuple(paths), str(exc))
    return UnknownRefs(tuple(lacking), tuple(missing), None, tuple(authors))
