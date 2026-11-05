"""What a session of ``kg serve`` may see: its clearance and audience, the visible set V_o of a build, and the store
it reads, opened read-only as that audience (``evo_agents.kg.serve``)."""

from __future__ import annotations

from evo_agents.kg.explore import Explorer
from evo_agents.kg.policy import Label, audience_below
from evo_agents.kg.project import Project
from evo_agents.kg.refs import stored
from evo_agents.kg.store import StaleSchema, Store

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
        self.plain = False  # kg_search ranks by plain BM25 alone, the public benches' baseline (Store.plain_search)
        self.plain_files: tuple | None = None  # (clone, commit): with ``plain``, rank whole files (kg.filebm25)
        # the stores the current call opened, closed when it answers: since Python 3.11 a connection left open waits
        # for the cycle collector (its statement cache refers back to it), and Windows cannot delete a file one holds
        self._opened: list[Store] | None = None
        # kg_search orders files by the learned model (evo_agents.kg.rank): None as the setting says, True or False always
        self.rank: bool | None = None
        self.explorer: Explorer | None = None  # kg_search explores (``evo_agents.kg.explore``): agents on the hub only
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
        """Whether an element labelled ``label`` (level, location, integrity and, from the store, audience) shows."""
        if self.sees_all:
            return True
        level, location, *_rest = label
        audience = label[3] if len(label) > 3 else frozenset()
        clearance = self.clearance
        assert clearance is not None, ANSWERS_NOTHING
        return level <= clearance[0] and location <= clearance[1] and audience_below(audience, self.audience)

    def seen(self, x: dict) -> bool:
        """``visible``, and not deprecated by a decision (``evo_agents.kg.pipeline.curation``) unless asked for."""
        return self.visible(x["label"]) and (self.deprecated or "deprecated" not in (x.get("props") or {}))

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

    def _store(self) -> tuple[Store, int]:
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
        return store, b

    def _graph(self) -> tuple[Store, int]:
        """The project's store, opened read-only: serving never writes, so it never waits on a build."""
        assert self.project is not None, ANSWERS_NOTHING
        try:
            store = Store.open_readonly(self.project)
        except StaleSchema as exc:
            raise ToolError(str(exc)) from None
        if store is not None and self._opened is not None:
            self._opened.append(store)
        b = store.latest_ready() if store else None
        if store is None or b is None:
            raise ToolError(f"project {self.project.name!r} has no graph yet: run `evo-agents kg sync --build`")
        return store, b

    def _lookup(self, store: Store, node_id: str, b: int) -> dict | None:
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
        return n if n is not None and self.visible(n["label"]) else None
