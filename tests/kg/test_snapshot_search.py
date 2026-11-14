"""An in-RAM snapshot of a build (``evo_agents.kg.snapshot``): the content hash a store gives the build, the ranking
of ``Store.search`` and the answers of kg_search, the reader's filter, the BM25 of FTS5 and its query syntax."""

import pytest

from evo_agents.kg import build as kg_build
from evo_agents.kg import clusters as kg_clusters
from evo_agents.kg import content
from evo_agents.kg import settings as kg_settings
from evo_agents.kg.ontology_rules import Violation, review_items
from evo_agents.kg.serve import Session
from evo_agents.kg.snapshot import Snapshot
from evo_agents.kg.snapshot_fts import QueryError, Table, code_words, parse, words
from evo_agents.kg.snapshot_search import SnapshotSearch, SnapshotSession
from evo_agents.kg.snapshot_text import Ranker
from evo_agents.kg.store import Store
from evo_agents.kg.store_clusters import Clusters
from tests.kg.test_store import make_searchable, sync_build

QUERIES = [
    "GraphBuilder",
    "graph builder",
    "buildgraph",
    "where does the hub build the knowledge graph on tuesdays",
    "đồ thị",
    "do thi",
    "duong ong",
    "search",
    "KB-01",
    "guide",
    "search.py",
    "app/search.py",
    "server",
]


def built(tmp_path, kg_env, **kw):
    project = make_searchable(tmp_path, kg_env, **kw)
    sync_build(project)
    store = Store.for_project(project)
    b = store.latest_ready()
    return project, store, b, Snapshot.from_store(store, b)


def results(answer: dict) -> list[str]:
    return [n["id"] for n in answer["structuredContent"]["results"]]


def test_the_content_hash_of_a_graph_is_the_one_its_build_has(tmp_path, kg_env, monkeypatch):
    """With clusters and a review queue too: the hash names the build without a store."""
    graphs = []
    write = Store.write

    def kept(self, graph, **kw):
        graphs.append(graph)
        return write(self, graph, **kw)

    def clustered(graph, policy, warm=None):
        first, second = sorted(graph.nodes)[:2]
        return Clusters(
            {"c1": {"level": 1, "parent": None, "label": "demo", "size": 2, "kinds": {"File": 2}}},
            {first: ("c1", None), second: ("c1", None)},
            {(1, "c1", "c2"): 0.5},
        )

    monkeypatch.setattr(Store, "write", kept)
    monkeypatch.setattr(kg_clusters, "available", lambda: True)
    monkeypatch.setattr(kg_clusters, "compute", clustered)
    monkeypatch.setattr(kg_build.kg_clusters, "coverage", lambda found: {"computed": True})
    violation = Violation("missing_prop", "app:file:app/search.py\x1fowner", ("app:file:app/search.py",), (), {})
    monkeypatch.setattr(kg_build.kg_review, "compute", lambda graph: review_items([violation]))
    project, store, b, snap = built(tmp_path, kg_env)
    graph = graphs[-1]
    assert graph.clusters is not None and graph.review, "the build has clusters and review items"
    stored = store.db.execute("SELECT content_hash FROM builds WHERE build_id = ?", (b,)).fetchone()[0]
    assert content.content_hash(graph) == stored == snap.content_hash
    assert Snapshot.from_graph(graph, b).content_hash == stored


def test_the_ranking_is_the_stores(tmp_path, kg_env):
    _, store, b, snap = built(tmp_path, kg_env)
    ranker = Ranker(snap.view(frozenset(), None, kg_settings.DEFAULT))
    for query in QUERIES:
        assert ranker.search(query, 0, limit=20) == store.search(query, b, limit=20), query
        scored = dict(store.search_scored(query, b, limit=20))
        for nid, score in ranker.search_scored(query, 0, limit=20):
            assert score == pytest.approx(scored[nid], abs=1e-9) if score is not None else scored[nid] is None


def test_kg_search_answers_as_a_session_does_for_each_clearance(tmp_path, kg_env):
    """The app's code is customer: the agent sink sees none of it, the wide one all. One query of many words differs
    by the arrangement locate gives a store's search (``evo_agents.kg.locate``), which a snapshot does not read."""
    project, _, _, snap = built(tmp_path, kg_env, app_level="customer")
    for sink in ("agent", "wide"):
        session = Session(project, sink)
        mine = SnapshotSession(SnapshotSearch(snap, session.clearance, session.audience))
        for query in QUERIES[:3] + QUERIES[4:]:
            args = {"query": query, "limit": 10}
            assert results(mine.call("kg_search", args)) == results(session.call("kg_search", args)), (sink, query)
    agent = Session(project, "agent")
    shown = results(
        SnapshotSession(SnapshotSearch(snap, agent.clearance, agent.audience)).call("kg_search", {"query": "search"})
    )
    assert shown and not any(nid.startswith(("app:", "symbol:app:")) for nid in shown)


def test_kg_search_reads_the_documents_an_entity_stands_for_and_the_code(tmp_path, kg_env):
    project, _, _, snap = built(tmp_path, kg_env)
    session = Session(project, "agent")
    mine = SnapshotSession(SnapshotSearch(snap, session.clearance, session.audience))
    for query in ("search", "GraphBuilder", "KB-01"):
        theirs = session.call("kg_search", {"query": query})["structuredContent"]
        ours = mine.call("kg_search", {"query": query})["structuredContent"]
        assert ours["mentioned_by"] == theirs["mentioned_by"], query
        assert ours["entities"] == theirs["entities"], query
        assert ours["code"] == theirs["code"], query
    assert mine.call("kg_node", {"id": "x"})["isError"]


def test_the_edges_of_a_node_come_in_the_stores_order_within_the_ceiling(tmp_path, kg_env):
    project, store, b, snap = built(tmp_path, kg_env, app_level="customer")
    nid = "app:file:app/search.py"
    full = snap.view(frozenset(), None, kg_settings.DEFAULT)
    assert [e["id"] for e in full.edges_of(nid)] == [e["id"] for e in store.edges_of(nid, b)]
    for direction in ("in", "out"):
        assert [e["id"] for e in full.edges_of(nid, direction=direction)] == [
            e["id"] for e in store.edges_of(nid, b, direction=direction)
        ]
    internal = snap.view(frozenset(), (1, 0), kg_settings.DEFAULT)
    assert full.edges_of(nid) and all(e["label"][0] <= 1 for e in internal.edges_of(nid))
    assert len(internal.edges_of(nid)) < len(full.edges_of(nid))
    assert full.edges_of(nid, limit=1) == full.edges_of(nid)[:1]


def test_the_snapshot_holds_every_live_node_and_edge(tmp_path, kg_env):
    _, store, b, snap = built(tmp_path, kg_env)
    nodes = store.db.execute("SELECT count(*) FROM nodes WHERE tx_from <= ? AND (tx_to IS NULL OR tx_to > ?)", (b, b))
    edges = store.db.execute("SELECT count(*) FROM edges WHERE tx_from <= ? AND (tx_to IS NULL OR tx_to > ?)", (b, b))
    assert len(snap.nodes) == nodes.fetchone()[0] and len(snap.edges) == edges.fetchone()[0]
    assert sum(len(p) for p in snap.partitions.values()) == len(snap.edges)
    with pytest.raises(AttributeError):
        snap.content_hash = "x"  # frozen


def test_tokens_fold_vietnamese_and_keep_code_identifiers_whole():
    assert words("Đường đi của dữ_liệu") == ["đuong", "đi", "cua", "du", "lieu"]  # as unicode61: đ is a letter
    assert words("Đồ thị") == ["đo", "thi"]
    assert code_words("build_graph(x) $ref my-name") == ["build_graph", "x", "$ref", "my-name"]


def test_bm25_is_fts5s_and_a_query_it_cannot_read_finds_nothing():
    table = Table([("alpha beta", ""), ("alpha", "alpha gamma"), ("delta", "")], words)
    found = dict(table.search('"alpha"', (1.0, 1.0)))
    assert set(found) == {0, 1} and all(score < 0 for score in found.values())
    assert set(dict(table.search('"alpha" AND "gamma"', (1.0, 1.0)))) == {1}
    assert set(dict(table.search('"beta" OR "delta"', (1.0, 1.0)))) == {0, 2}
    assert set(dict(table.search('"gam"*', (1.0, 1.0)))) == {1}
    assert set(dict(table.search('"alpha gamma"', (1.0, 1.0)))) == {1}
    assert table.search('"alpha" AND', (1.0, 1.0)) == []
    with pytest.raises(QueryError):
        parse('("alpha"')
