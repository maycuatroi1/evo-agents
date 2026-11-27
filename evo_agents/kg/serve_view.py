"""What a session of the kg_* tools may see: its clearance and audience, the visible set V_o of a build, and the graph
it reads as that audience (``evo_agents.kg.serve``). A session reads the graph its subclass gives (``_graph``): the
hub's snapshot of its tables (``evo_agents.hub.kg_graph.HubSession``), or a snapshot of the rows of a build for
the tests and benches (``evo_agents.kg.bench.rows_file.RowsSession``); this one reads none."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace

from evo_agents.kg.explore import Explorer
from evo_agents.kg.graph_scope import DEFAULT_GRAPH, GraphScope
from evo_agents.kg.project import Project
from evo_agents.kg.refs import stored
from evo_agents.kg.snapshot import SnapshotView
from evo_agents.kg.visibility import Visibility
from evo_agents.schema.policy import Label

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
        self.given: Counter = (
            Counter()
        )  # node id: times kg_context gave it in this session (evo_agents.kg.context_pack)
        self.last_context: tuple[tuple, Counter] | None = (
            None  # the last kg_context: its request, what was given before
        )
        self.handles: dict[str, tuple] = {}  # handle: (made, chunks left, audience its result revealed)
        self.policy = project.policy if project else None
        # what the session may see (evo_agents.kg.visibility): the call's, and the store's once a tool reads one
        self.visibility = Visibility()
        self.read: Label | None = None  # join of the labels the current call reveals
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

    # -- the reader ------------------------------------------------------------------------------

    @property
    def clearance(self) -> tuple[int, int] | None:
        """The (level, location) the session's sink may read; None for a session with an error."""
        return self.visibility.clearance

    @clearance.setter
    def clearance(self, clearance: tuple[int, int] | None) -> None:
        self.visibility = replace(self.visibility, clearance=clearance)

    @property
    def audience(self) -> frozenset[str]:
        """The principals the reader is (``evo_agents.schema.policy``)."""
        return self.visibility.audience

    @audience.setter
    def audience(self, audience: frozenset[str]) -> None:
        self.visibility = replace(self.visibility, audience=audience)

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

    def saw(self, label: tuple) -> None:
        """Join ``label`` to what the current call reveals (``read``)."""
        assert self.project is not None, ANSWERS_NOTHING
        seen = Label.of(label, frozenset({self.project.name}))
        self.read = seen if self.read is None else self.read.join(seen)

    def store(self) -> SnapshotView:
        """The graph this call reads, as the session's reader reads it (``Visibility.reading``): when the highest
        label of its build is within the clearance and its audiences within the session's, every element is visible,
        and ``visibility`` answers so without comparing labels."""
        store = self._graph()
        assert self.clearance is not None, ANSWERS_NOTHING
        self.visibility = self.visibility.reading(store)
        return store

    def _graph(self) -> SnapshotView:
        """The graph this call reads, of a ready build: the subclass's. This machine holds none."""
        raise self.no_graph()

    def no_graph(self) -> ToolError:
        """What a call answers when this machine holds no graph of the project."""
        assert self.project is not None, ANSWERS_NOTHING
        return ToolError(
            f"project {self.project.name!r} has no graph on this machine: graphs are built on the hub,"
            " which kg serve reads signed in"
        )

    def lookup(self, store: SnapshotView, node_id: str) -> dict | None:
        """A visible node by its stored id, its readable id (``refs.stored``) or an alias; None alike for a missing
        node and a hidden one."""
        if not isinstance(node_id, str) or not node_id:
            return None
        n = None
        for candidate in stored(node_id):
            n = store.node(candidate)
            if n is not None:
                break
        if n is None:
            alias = store.alias_of(node_id)
            n = store.node(alias) if alias else None
        return n if n is not None and self.visibility.shows(n) else None

    def locked(self, store: SnapshotView, node_id: str) -> dict | None:
        """The node ``node_id`` names when it is above the clearance and asks to show as locked (``props.locked``, as
        the docs plugin's typed nodes do): of the reader's audience and the named graph the call reads, but of a level
        or location the clearance does not reach. None for any other, a missing node among them. Only its id, kind
        and label may be answered of it: the audience of the reader leaves out the nodes of a space they are not in
        before this is asked (``SnapshotView.node``)."""
        if not isinstance(node_id, str) or not node_id:
            return None
        n = next((x for x in (store.node(c) for c in stored(node_id)) if x is not None), None)
        if n is None or (n.get("props") or {}).get("locked") is not True or not self.visibility.scoped(n):
            return None
        return n if self.visibility.holds_audience(n) else None
