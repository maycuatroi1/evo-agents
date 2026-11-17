"""What a session of the kg_* tools may see: its clearance and audience, the visible set V_o of a build, and the graph
it reads as that audience (``evo_agents.kg.serve``). A session reads the graph its subclass gives (``_graph``): the
hub's snapshot of its tables (``evo_agents.hub.kg_graph.HubSession``), or a snapshot of the rows of a build for
the tests and benches (``evo_agents.kg.bench.rows_file.RowsSession``); this one reads none."""

from __future__ import annotations

from evo_agents.kg.explore import Explorer
from evo_agents.kg.graph_scope import DEFAULT_GRAPH, GraphScope
from evo_agents.kg.policy import Label, audience_below
from evo_agents.kg.project import Project
from evo_agents.kg.refs import stored
from evo_agents.kg.snapshot import SnapshotView

# Only a session with an ``error`` has no project, policy or clearance, and it answers nothing but that error.
ANSWERS_NOTHING = "a session with an error reads nothing"


class ToolError(Exception):
    pass


class SessionView:
    """The state of a session and its checks of what it may read; the tools are ``evo_agents.kg.serve.Session``."""

    def __init__(self, project: Project | None, sink: str, *, error: str | None = None):
        self.project = project
        self.sink = sink
        self.error = error
        self.handles: dict[str, tuple] = {}  # handle: (made, chunks left, audience its result revealed)
        self.policy = project.policy if project else None
        self.clearance = None
        self.sees_all = False  # this call's build holds no label above the clearance (``_store``)
        self.read: Label | None = None  # join of the labels the current call reveals
        self.audience: frozenset[str] = frozenset()  # principals the reader is (evo_agents.kg.policy)
        self.deprecated = False  # this call shows the deprecated nodes and edges too (``seen``)
        # kg_search ranks by meaning too (evo_agents.kg.semantic): None as the graph's mode says, True or False always
        self.semantic: bool | None = None
        self.plain = False  # kg_search ranks by plain BM25 alone, the public benches' baseline (``plain_search``)
        self.plain_files: tuple | None = None  # (clone, commit): with ``plain``, rank whole files (kg.filebm25)
        # kg_search orders files by the learned model (evo_agents.kg.rank): None as the setting says, True or False always
        self.rank: bool | None = None
        self.explorer: Explorer | None = None  # kg_search explores (``evo_agents.kg.explore``): agents on the hub only
        # the named graphs this session knows, name to sources (``evo_agents.kg.graph_scope``): none on this machine,
        # those the hub resolved for the call on the hub; ``scope`` is the one the current call reads, None for all
        self.named_graphs: dict[str, frozenset[str]] = {}
        self.scope: GraphScope | None = None
        if project is not None and error is None:
            policy = project.policy
            if sink == "cli" and "cli" not in policy.sinks:
                # The local operator already holds the corpus; the cli sink sees everything unless declared.
                self.clearance = (len(policy.levels) - 1, len(policy.locations) - 1)
            else:
                try:
                    clr = policy.clearance(sink)
                    self.clearance = (clr.level, clr.location)
                except KeyError as exc:
                    self.error = str(exc.args[0])

    # -- visibility -----------------------------------------------------------------------------

    def visible(self, label: tuple) -> bool:
        """Whether an element labelled ``label`` (level, location, integrity and, from the build's rows, audience)
        shows."""
        if self.sees_all:
            return True
        level, location, *_rest = label
        audience = label[3] if len(label) > 3 else frozenset()
        clearance = self.clearance
        assert clearance is not None, ANSWERS_NOTHING
        return level <= clearance[0] and location <= clearance[1] and audience_below(audience, self.audience)

    def seen(self, x: dict) -> bool:
        """``visible``, and not deprecated by a decision (``evo_agents.kg.pipeline.curation``) unless asked for, and
        of the named graph the call reads (``scoped``)."""
        return (
            self.visible(x["label"])
            and (self.deprecated or "deprecated" not in (x.get("props") or {}))
            and self.scoped(x)
        )

    def scoped(self, x: dict) -> bool:
        """Whether a node or an edge row is of the named graph the call reads; any one without a named graph."""
        return self.scope is None or self.scope.holds(x)

    def shows(self, node: dict) -> bool:
        """``visible`` for a node row, and of the named graph the call reads."""
        return self.visible(node["label"]) and self.scoped(node)

    def named_scope(self, graph) -> GraphScope | None:
        """The scope of the named graph a call passes as ``graph``: None for none or ``default`` (every source).
        Raises ToolError for a name the session does not know."""
        if graph is None or (isinstance(graph, str) and graph.strip() in ("", DEFAULT_GRAPH)):
            return None
        name = graph.strip() if isinstance(graph, str) else graph
        if isinstance(name, str) and name in self.named_graphs:
            return GraphScope(name, self.named_graphs[name])
        raise ToolError(self.unknown_graph(str(graph)))

    def unknown_graph(self, name: str) -> str:
        """The error of a call naming a graph the session does not know."""
        return (
            f"no knowledge graph named {name!r} here: named graphs are kept on the hub (`evo-agents hub kg graph "
            f"list`); {DEFAULT_GRAPH!r} reads every source"
        )

    def describe(self, label: tuple) -> str:
        assert self.policy is not None, ANSWERS_NOTHING
        lvl = self.policy.level_name(label[0])
        loc = self.policy.location_name(label[1])
        integrity = label[2]
        audience = " audience " + " ".join(sorted(label[3])) if len(label) > 3 and label[3] else ""
        return f"{lvl}{'' if loc == 'any' else '/' + loc},{integrity}{audience}"

    def _saw(self, label: tuple) -> None:
        assert self.project is not None, ANSWERS_NOTHING
        seen = Label.of(label, frozenset({self.project.name}))
        self.read = seen if self.read is None else self.read.join(seen)

    def _store(self) -> tuple[SnapshotView, int]:
        """The graph this call reads, as the session's audience reads it, and its latest ready build. When the highest
        label of that build is within the clearance and its audiences within the session's, every element is
        visible, and ``visible`` answers so without comparing labels."""
        store, b = self._graph()
        clearance = self.clearance
        assert clearance is not None, ANSWERS_NOTHING
        store.reader = self.audience
        store.asked_clearance = clearance  # what agents asked before reading a node, under this clearance only
        top = store.top_label(b)
        within = top is not None and top[0] <= clearance[0] and top[1] <= clearance[1]
        self.sees_all = within and audience_below(store.top_audience(b), self.audience)
        store.ceiling = None if self.sees_all else clearance  # edges above it are left out before the limit
        if self.scope is not None:  # a named graph leaves out nodes the label lets through: read further, as then
            self.scope.bind(store, b)
            self.sees_all = False
        return store, b

    def _graph(self) -> tuple[SnapshotView, int]:
        """The graph this call reads and its latest ready build: the subclass's. This machine holds none."""
        raise self.no_graph()

    def no_graph(self) -> ToolError:
        """What a call answers when this machine holds no graph of the project."""
        assert self.project is not None, ANSWERS_NOTHING
        return ToolError(
            f"project {self.project.name!r} has no graph on this machine: graphs are built on the hub,"
            " which kg serve reads signed in"
        )

    def _lookup(self, store: SnapshotView, node_id: str, b: int) -> dict | None:
        """A visible node by its stored id, its readable id (``refs.stored``) or an alias; None alike for a missing
        node and a hidden one."""
        if not isinstance(node_id, str) or not node_id:
            return None
        n = None
        for candidate in stored(node_id):
            n = store.node(candidate, b)
            if n is not None:
                break
        if n is None:
            alias = store.alias_of(node_id, b)
            n = store.node(alias, b) if alias else None
        return n if n is not None and self.shows(n) else None
