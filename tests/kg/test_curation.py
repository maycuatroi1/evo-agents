"""Curation: the decisions writers took on the hub, as the next build applies them at the binding stage and as the
kg tools then show the graph (``evo_agents.kg.pipeline.curation``, ``evo_agents.hub.kg_decisions``)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

pytest.importorskip("sqlalchemy", reason="the hub's source of decisions needs the hub-server extra")

from evo_agents.hub import kg_decisions
from evo_agents.hub.kg_sources import HUB, checked, keep_sources, knowledge_with
from evo_agents.kg.build import build_project
from evo_agents.kg.pipeline.curation import curation_item
from evo_agents.kg.project import load_project_at
from evo_agents.kg.serve import Session
from evo_agents.kg.store import Store, edge_id
from evo_agents.kg.sync import sync_project
from tests.kg.projects import make_project

RANK = "symbol:app:app/search.py::rank_results"
SEARCH = "symbol:app:app/search.py::search"
HANDLE = "symbol:app:app/server.py::Server.handle"
SEARCH_PY = "app:file:app/search.py"
SERVER_PY = "app:file:app/server.py"
GUIDE = "app:file:docs/guide.md"
OLD = "app:file:docs/old.md"
LABEL = {"level": "internal", "integrity": "U", "projects": ["demo"]}


def row(number: int, kind: str, src: str, rel: str | None = None, dst: str | None = None, reason: str = "why"):
    return SimpleNamespace(number=number, kind=kind, src=src, rel=rel, dst=dst, reason=reason)


def curated(tmp_path, home, rows: list):
    """The demo project built with the hub's source of the decisions ``rows``: (project, store, build, report)."""
    project = make_project(tmp_path, home)
    sync_project(project)
    path = project.harness.root / "knowledge.yaml"
    knowledge = yaml.safe_load(path.read_text(encoding="utf-8"))
    answer = {"items": [kg_decisions.curation_item(rows, LABEL)], "identifiers": []}
    source = checked(kg_decisions.SOURCE, HUB, answer, knowledge, "demo")
    path.write_text(yaml.safe_dump(knowledge_with(knowledge, [source]), sort_keys=True), encoding="utf-8")
    project = load_project_at(project.harness.root, home)
    keep_sources(project, [source])
    report = build_project(project)
    assert report.ok, report.errors
    store = Store.for_project(project)
    store.reader = frozenset({"*"})
    return project, store, report.build_id, report


DECISIONS = [
    row(1, "merge", OLD, dst=GUIDE),
    row(2, "link", SERVER_PY, "verifies", SEARCH_PY),
    row(3, "deprecate", RANK, reason="ranking moved to the index"),
    row(4, "deprecate", HANDLE, "calls", SEARCH, reason="the server calls the index now"),
]


def test_curation_merges_links_and_deprecates_at_the_binding_stage(tmp_path, kg_env):
    _project, store, b, report = curated(tmp_path, kg_env, DECISIONS)
    # A merge is a same_as: the smallest id stays and the other is its alias.
    assert store.node(OLD, b) is None and store.alias_of(OLD, b) == GUIDE
    assert OLD in store.node(GUIDE, b)["aliases"]
    # A link is a declared edge that names its decision.
    added = next(e for e in store.edges_of(SERVER_PY, b, "out") if e["rel"] == "verifies")
    assert added["dst"] == SEARCH_PY and added["status"] == "declared" and added["props"] == {"decision": 2}
    # A deprecation marks the node or edge it names, with its reason and decision; neither leaves the graph.
    assert store.node(RANK, b)["props"]["deprecated"] == {"reason": "ranking moved to the index", "decision": 3}
    calls = next(e for e in store.edges_of(HANDLE, b, "out") if e["rel"] == "calls")
    assert calls["id"] == edge_id(HANDLE, "calls", SEARCH)
    assert calls["props"]["deprecated"] == {"reason": "the server calls the index now", "decision": 4}
    assert report.coverage["curation"] == {"deprecated": 2, "orphans": 0}


def test_curation_orphans_are_skipped_and_never_break_the_build(tmp_path, kg_env):
    gone = "app:file:app/gone.py"
    rows = [
        row(1, "merge", gone, dst=GUIDE),
        row(2, "link", gone, "verifies", SEARCH_PY),
        row(3, "deprecate", gone),
        row(4, "deprecate", HANDLE, "imports", SEARCH),  # no such edge
        row(5, "deprecate", RANK),
    ]
    _project, store, b, report = curated(tmp_path, kg_env, rows)
    assert store.node(gone, b) is None and store.alias_of(gone, b) is None
    assert not [e for e in store.edges_of(SEARCH_PY, b, "in") if e["rel"] == "verifies"]
    assert report.coverage["curation"] == {"deprecated": 1, "orphans": 2}
    assert store.node(RANK, b)["props"]["deprecated"]["decision"] == 5


def test_curation_item_of_unreadable_or_foreign_entries_gives_no_fact():
    index = {"ids": {RANK}, "url": {}, "symbol": {}, "step": {}, "repo": {}, "path": {}, "seam": {}, "plan": {}}
    record = {"id": kg_decisions.ITEM, "kind": "binding", "props": {"curation": True}}
    assert curation_item(record, ": not yaml [", index) == []
    assert curation_item(record, "decisions: [1, {kind: rename, decision: 2}]", index) == []
    facts = curation_item(record, "decisions: [{decision: 3, kind: deprecate, id: x, reason: r}]", index)
    assert facts == [{"t": "deprecate", "id": "x", "reason": "r", "decision": 3}]


def test_deprecated_parts_are_left_out_of_search_context_path_and_impact_unless_asked(tmp_path, kg_env):
    project, _store, _b, _report = curated(tmp_path, kg_env, DECISIONS)
    session = Session(project, "agent")

    def text(name: str, args: dict) -> str:
        found = session.call(name, args)
        assert not found.get("isError"), found
        return found["content"][0]["text"]

    shown = "::rank_results]"  # the node's id as the tools print it, not the query they echo
    assert shown not in text("kg_search", {"query": "rank_results"})
    assert shown in text("kg_search", {"query": "rank_results", "deprecated": True})
    assert shown not in text("kg_context", {"ids": [SEARCH], "hops": 1})
    assert shown in text("kg_context", {"ids": [SEARCH], "hops": 1, "deprecated": True})
    # kg_impact does not walk the deprecated call from Server.handle to search.
    assert "Server.handle" not in text("kg_impact", {"id": SEARCH, "depth": 1})
    assert "Server.handle" in text("kg_impact", {"id": SEARCH, "depth": 1, "deprecated": True})
    hidden = session.call("kg_path", {"from": HANDLE, "to": RANK})
    assert hidden.get("isError") or "no path" in hidden["content"][0]["text"]
    assert shown in text("kg_path", {"from": HANDLE, "to": RANK, "deprecated": True})


def test_kg_node_states_the_deprecation_and_its_reason(tmp_path, kg_env):
    project, _store, _b, _report = curated(tmp_path, kg_env, DECISIONS)
    session = Session(project, "agent")
    found = session.call("kg_node", {"id": RANK})
    text = found["content"][0]["text"]
    assert "deprecated: ranking moved to the index (decision 3)" in text
    assert found["structuredContent"]["node"]["props"]["deprecated"]["decision"] == 3
