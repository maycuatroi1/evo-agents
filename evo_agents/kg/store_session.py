"""The kg_* tools over a graph file of this machine (``StoreSession``): what the tests and the benches read a graph
they built with, the SQLite half of ``evo_agents.kg.serve.Session``, which goes with the graph store. The hub answers
the tools from its snapshot of its tables (``evo_agents.hub.kg_graph.HubSession``) and never opens such a file.

The file is opened read-only: serving never writes, so it never waits on a build. The stores a call opened are closed
when it answers: since Python 3.11 a connection left open waits for the cycle collector (its statement cache refers
back to it), and Windows cannot delete a file one holds.
"""

from __future__ import annotations

import sqlite3

from evo_agents.kg.serve import Session
from evo_agents.kg.serve_view import ANSWERS_NOTHING, ToolError
from evo_agents.kg.store import StaleSchema, Store


class StoreSession(Session):
    """A Session over the project's graph file, ``graph.sqlite`` in its home."""

    read_errors = (sqlite3.Error,)

    def _graph(self) -> tuple[Store, int]:  # type: ignore[override]  # a Store reads as a SnapshotView does
        assert self.project is not None, ANSWERS_NOTHING
        try:
            store = Store.open_readonly(self.project)
        except StaleSchema as exc:
            raise ToolError(str(exc)) from None
        if store is not None and self._opened is not None:
            self._opened.append(store)
        b = store.latest_ready() if store else None
        if store is None or b is None:
            raise self.no_graph()
        return store, b
