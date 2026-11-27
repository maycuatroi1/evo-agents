"""An in-RAM snapshot of a build (``evo_agents.kg.snapshot``): the content hash a build gives the graph, the ranking
and the kg_search answers a graph file gave the same build (kept here as literal expected values), the reader's
filter, the BM25 of FTS5 and its query syntax."""

import pytest

from evo_agents.kg import build as kg_build
from evo_agents.kg import clusters as kg_clusters
from evo_agents.kg import content
from evo_agents.kg import graph_rows as kg_graph_rows
from evo_agents.kg import settings as kg_settings
from evo_agents.kg.graph_model import Clusters
from evo_agents.kg.ontology_rules import Violation, review_items
from evo_agents.kg.snapshot import Snapshot
from evo_agents.kg.snapshot_fts import QueryError, Table, code_words, parse, words
from evo_agents.kg.snapshot_search import SnapshotSearch, SnapshotSession
from evo_agents.kg.snapshot_text import Ranker
from tests.kg.built import rows_of, snapshot_of, sync_build
from tests.kg.hub_graph import hub_session
from tests.kg.test_store import make_searchable
from tests.kg.test_walk import entity_project, walk_on

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

# What the search and kg_search of a local session gave on the graph file of the same build, before the graph
# file went: the snapshot answers as it did.
RANKING = {
    "GraphBuilder": [
        "symbol:app:app/hub_build.py::GraphBuilder",
        "symbol:app:app/hub_build.py::GraphBuilder.buildGraph",
        "app:file:app/hub_build.py",
    ],
    "graph builder": [
        "symbol:app:app/hub_build.py::GraphBuilder.buildGraph",
        "symbol:app:app/hub_build.py::GraphBuilder",
        "app:file:app/hub_build.py",
    ],
    "buildgraph": ["symbol:app:app/hub_build.py::GraphBuilder.buildGraph"],
    "where does the hub build the knowledge graph on tuesdays": [
        "app:file:app/hub_build.py",
        "symbol:app:app/hub_build.py::GraphBuilder.buildGraph",
        "symbol:app:app/hub_build.py::GraphBuilder",
        "symbol:app:app/hub_build.py::load_corpus",
        "harness:file:knowledge.yaml",
        "harness:file:harness.yaml",
    ],
    "đồ thị": ["app:file:docs/do-thi.md", "app:file:docs/do-thi.md#đồ-thị-tri-thức"],
    "do thi": ["app:file:docs/do-thi.md", "app:file:docs/do-thi.md#đồ-thị-tri-thức"],
    "duong ong": ["app:file:docs/plain.md#ghi-chu", "app:file:docs/do-thi.md#đồ-thị-tri-thức"],
    "search": [
        "symbol:app:app/search.py::search",
        "app:file:app/search.py",
        "seam:search-api",
        "symbol:app:app/search.py::rank_results",
        "harness:file:bindings/core.yaml",
        "harness:file:contracts.yaml",
        "app:file:docs/guide.md#guide",
        "app:file:docs/guide.md#setup",
        "plan:demo",
        "plan:demo/step:2",
        "plan:demo/step:1",
        "app:commit:a41ca15bc40bc2b9b8dac7aa66a38311b42c1d8a",
    ],
    "KB-01": [
        "usecase:KB-01",
        "harness:file:bindings/core.yaml",
        "app:file:docs/guide.md#guide",
        "plan:demo",
        "usecase:KB-02",
        "app:file:docs/guide.md#usage",
        "harness:file:knowledge.yaml",
    ],
    "guide": [
        "app:file:docs/guide.md",
        "app:file:docs/guide.md#guide",
        "app:file:docs/guide.md#details",
        "app:file:docs/guide.md#usage",
        "app:file:docs/guide.md#setup",
        "harness:file:CLUSTER.md#demo",
    ],
    "search.py": [
        "app:file:app/search.py",
        "symbol:app:app/search.py::search",
        "symbol:app:app/search.py::rank_results",
        "seam:search-api",
        "harness:file:bindings/core.yaml",
        "harness:file:contracts.yaml",
        "app:file:docs/guide.md#guide",
        "app:file:docs/guide.md#setup",
        "plan:demo/step:2",
        "plan:demo/step:1",
        "app:file:app/__init__.py",
        "app:file:app/server.py",
        "app:file:app/hub_build.py",
        "symbol:app:app/server.py::Server",
        "symbol:app:app/server.py::Server.handle",
        "symbol:app:app/hub_build.py::load_corpus",
        "symbol:app:app/hub_build.py::GraphBuilder.buildGraph",
        "symbol:app:app/hub_build.py::GraphBuilder",
        "plan:demo",
        "app:file:docs/old.md#old",
    ],
    "app/search.py": [
        "app:file:app/search.py",
        "symbol:app:app/search.py::search",
        "symbol:app:app/search.py::rank_results",
        "harness:file:contracts.yaml",
        "harness:file:bindings/core.yaml",
        "app:file:docs/guide.md#guide",
        "app:file:docs/guide.md#setup",
        "plan:demo/step:2",
        "plan:demo/step:1",
        "seam:search-api",
        "app:file:app/__init__.py",
        "app:file:app/server.py",
        "app:file:app/hub_build.py",
        "symbol:app:app/server.py::Server",
        "symbol:app:app/server.py::Server.handle",
        "symbol:app:app/hub_build.py::load_corpus",
        "symbol:app:app/hub_build.py::GraphBuilder.buildGraph",
        "symbol:app:app/hub_build.py::GraphBuilder",
        "plan:demo",
        "app:file:docs/old.md#old",
        "repo:app",
        "source:app",
        "harness:file:harness.yaml",
        "harness:file:knowledge.yaml",
        "dir:app:app",
        "commit:app@1a2b3c4d",
        "app:commit:a41ca15bc40bc2b9b8dac7aa66a38311b42c1d8a",
        "app:commit:de2d05e9a1087df1db5bf5718ae77dbf99eca5ba",
        "harness:file:CLUSTER.md#demo",
    ],
    "server": [
        "symbol:app:app/server.py::Server",
        "symbol:app:app/server.py::Server.handle",
        "app:file:app/server.py",
        "plan:demo/step:2",
        "app:file:docs/guide.md#setup",
        "app:file:docs/old.md#old",
    ],
}
TOP_SCORES = {
    "GraphBuilder": [
        ("symbol:app:app/hub_build.py::GraphBuilder", None),
        ("symbol:app:app/hub_build.py::GraphBuilder.buildGraph", 35.157507),
        ("app:file:app/hub_build.py", 3.415203),
    ],
    "graph builder": [
        ("symbol:app:app/hub_build.py::GraphBuilder.buildGraph", 22.955972),
        ("symbol:app:app/hub_build.py::GraphBuilder", 22.564129),
        ("app:file:app/hub_build.py", 3.415203),
    ],
    "buildgraph": [("symbol:app:app/hub_build.py::GraphBuilder.buildGraph", 14.354826)],
    "where does the hub build the knowledge graph on tuesdays": [
        ("app:file:app/hub_build.py", 16.486139),
        ("symbol:app:app/hub_build.py::GraphBuilder.buildGraph", 14.477004),
        ("symbol:app:app/hub_build.py::GraphBuilder", 13.282433),
    ],
    "đồ thị": [("app:file:docs/do-thi.md", 24.950792), ("app:file:docs/do-thi.md#đồ-thị-tri-thức", 17.087729)],
    "do thi": [("app:file:docs/do-thi.md", 12.475396), ("app:file:docs/do-thi.md#đồ-thị-tri-thức", 8.543864)],
    "duong ong": [("app:file:docs/plain.md#ghi-chu", 12.466613), ("app:file:docs/do-thi.md#đồ-thị-tri-thức", 2.642377)],
    "search": [
        ("symbol:app:app/search.py::search", None),
        ("app:file:app/search.py", 5.355187),
        ("seam:search-api", 5.339546),
    ],
    "KB-01": [
        ("usecase:KB-01", None),
        ("harness:file:bindings/core.yaml", 10.366369),
        ("app:file:docs/guide.md#guide", 7.645777),
    ],
    "guide": [
        ("app:file:docs/guide.md", 8.21067),
        ("app:file:docs/guide.md#guide", 5.632321),
        ("app:file:docs/guide.md#details", 4.841934),
    ],
    "search.py": [
        ("app:file:app/search.py", 11.933471),
        ("symbol:app:app/search.py::search", 7.678271),
        ("symbol:app:app/search.py::rank_results", 6.953156),
    ],
    "app/search.py": [
        ("app:file:app/search.py", None),
        ("symbol:app:app/search.py::search", 11.517413),
        ("symbol:app:app/search.py::rank_results", 10.429739),
    ],
    "server": [
        ("symbol:app:app/server.py::Server", 8.254877),
        ("symbol:app:app/server.py::Server.handle", 8.232714),
        ("app:file:app/server.py", 8.21067),
    ],
}
PARTS = {
    "search": {
        "mentioned_by": {"app:file:app/server.py": "app:file:docs/guide.md#setup"},
        "entities": {},
        "code": {"app:file:app/search.py": [5], "app:file:app/server.py": [1, 6]},
    },
    "GraphBuilder": {"mentioned_by": {}, "entities": {}, "code": {"app:file:app/hub_build.py": [4]}},
    "KB-01": {
        "mentioned_by": {"symbol:app:app/search.py::search": "app:file:docs/guide.md#guide"},
        "entities": {},
        "code": {},
    },
}
KG_SEARCH = {
    "agent": {
        "GraphBuilder": [],
        "graph builder": [],
        "buildgraph": [],
        "đồ thị": [],
        "do thi": [],
        "duong ong": [],
        "search": [
            "seam:search-api",
            "harness:file:bindings/core.yaml",
            "harness:file:contracts.yaml",
            "plan:demo",
            "plan:demo/step:2",
            "plan:demo/step:1",
        ],
        "KB-01": ["usecase:KB-01", "harness:file:bindings/core.yaml", "plan:demo", "harness:file:knowledge.yaml"],
        "guide": ["harness:file:CLUSTER.md#demo"],
        "search.py": [
            "seam:search-api",
            "harness:file:bindings/core.yaml",
            "harness:file:contracts.yaml",
            "plan:demo/step:2",
            "plan:demo/step:1",
            "plan:demo",
        ],
        "app/search.py": [
            "harness:file:contracts.yaml",
            "harness:file:bindings/core.yaml",
            "plan:demo/step:2",
            "plan:demo/step:1",
            "seam:search-api",
            "plan:demo",
            "repo:app",
            "harness:file:harness.yaml",
            "harness:file:knowledge.yaml",
            "commit:app@1a2b3c4d",
        ],
        "server": ["plan:demo/step:2"],
    },
    "wide": {
        "GraphBuilder": [
            "symbol:app:app/hub_build.py::GraphBuilder",
            "symbol:app:app/hub_build.py::GraphBuilder.buildGraph",
            "app:file:app/hub_build.py",
        ],
        "graph builder": [
            "symbol:app:app/hub_build.py::GraphBuilder.buildGraph",
            "symbol:app:app/hub_build.py::GraphBuilder",
            "app:file:app/hub_build.py",
        ],
        "buildgraph": ["symbol:app:app/hub_build.py::GraphBuilder.buildGraph", "app:file:app/hub_build.py"],
        "đồ thị": ["app:file:docs/do-thi.md", "app:file:docs/do-thi.md#đồ-thị-tri-thức"],
        "do thi": ["app:file:docs/do-thi.md", "app:file:docs/do-thi.md#đồ-thị-tri-thức"],
        "duong ong": ["app:file:docs/plain.md#ghi-chu", "app:file:docs/do-thi.md#đồ-thị-tri-thức"],
        "search": [
            "symbol:app:app/search.py::search",
            "app:file:app/search.py",
            "seam:search-api",
            "symbol:app:app/search.py::rank_results",
            "harness:file:bindings/core.yaml",
            "harness:file:contracts.yaml",
            "app:file:docs/guide.md#guide",
            "app:file:docs/guide.md#setup",
            "app:file:app/server.py",
            "plan:demo",
        ],
        "KB-01": [
            "usecase:KB-01",
            "harness:file:bindings/core.yaml",
            "app:file:docs/guide.md#guide",
            "symbol:app:app/search.py::search",
            "plan:demo",
            "usecase:KB-02",
            "app:file:docs/guide.md#usage",
            "harness:file:knowledge.yaml",
        ],
        "guide": [
            "app:file:docs/guide.md",
            "app:file:docs/guide.md#guide",
            "symbol:app:app/search.py::search",
            "app:file:docs/guide.md#details",
            "symbol:app:app/search.py::rank_results",
            "app:file:docs/guide.md#usage",
            "app:file:docs/guide.md#setup",
            "app:file:app/server.py",
            "harness:file:CLUSTER.md#demo",
        ],
        "search.py": [
            "app:file:app/search.py",
            "symbol:app:app/search.py::search",
            "symbol:app:app/search.py::rank_results",
            "seam:search-api",
            "harness:file:bindings/core.yaml",
            "harness:file:contracts.yaml",
            "app:file:docs/guide.md#guide",
            "app:file:docs/guide.md#setup",
            "app:file:app/server.py",
            "plan:demo/step:2",
        ],
        "app/search.py": [
            "app:file:app/search.py",
            "symbol:app:app/search.py::search",
            "symbol:app:app/search.py::rank_results",
            "harness:file:contracts.yaml",
            "harness:file:bindings/core.yaml",
            "app:file:docs/guide.md#guide",
            "app:file:docs/guide.md#setup",
            "app:file:app/server.py",
            "plan:demo/step:2",
            "plan:demo/step:1",
        ],
        "server": [
            "symbol:app:app/server.py::Server",
            "symbol:app:app/server.py::Server.handle",
            "app:file:app/server.py",
            "plan:demo/step:2",
            "app:file:docs/guide.md#setup",
            "app:file:docs/old.md#old",
        ],
    },
}
EDGES = {
    "both": [
        "e:ab1436e838f406df",
        "e:5fcf2ed5831d5899",
        "e:077ed510d27ccdf4",
        "e:8e5097cd956b0c3a",
        "e:010b91c15b79493e",
        "e:2110b6898d3ff46b",
        "e:0fcae825595f15a6",
        "e:92f0dedd5e8b8a3c",
    ],
    "in": ["e:ab1436e838f406df", "e:077ed510d27ccdf4", "e:2110b6898d3ff46b", "e:92f0dedd5e8b8a3c"],
    "out": ["e:5fcf2ed5831d5899", "e:8e5097cd956b0c3a", "e:010b91c15b79493e", "e:0fcae825595f15a6"],
}
BENCH = {
    "zephyr launcher": [
        "app:file:docs/alpha/launch.md#launch",
        "app:file:docs/alpha/launch.md",
        "app:commit:0455378444a11282a9271875ad59adbb7aedc376",
        "app:file:docs/beta/tail.md",
        "dir:app:docs/alpha",
        "entity:Concept:zephyr",
        "entity:Concept:quasar",
    ],
    "Quasar": [
        "app:file:docs/alpha/launch.md",
        "app:file:docs/beta/tail.md",
        "app:commit:95f05be10ce4340224523795e44db964d621205e",
        "app:commit:0455378444a11282a9271875ad59adbb7aedc376",
        "app:file:docs/beta/tail.md#tail",
        "dir:app:docs/beta",
        "app:file:docs/alpha/launch.md#launch",
        "dir:app:docs/alpha",
        "entity:Concept:quasar",
        "entity:Concept:zephyr",
    ],
    "comet tail": [
        "app:file:docs/beta/tail.md#tail",
        "app:file:docs/beta/tail.md",
        "app:commit:95f05be10ce4340224523795e44db964d621205e",
        "dir:app:docs/beta",
        "app:file:docs/alpha/launch.md",
        "dir:app:docs",
        "entity:Concept:quasar",
        "entity:Concept:zephyr",
    ],
    "engine": [
        "app:file:docs/alpha/launch.md#launch",
        "app:file:docs/alpha/launch.md",
        "app:commit:0455378444a11282a9271875ad59adbb7aedc376",
        "dir:app:docs/alpha",
        "app:file:docs/beta/tail.md",
        "entity:Concept:quasar",
        "entity:Concept:zephyr",
    ],
}


def built(tmp_path, kg_env, **kw):
    project = make_searchable(tmp_path, kg_env, **kw)
    sync_build(project)
    return project, snapshot_of(project)


def reader(project, snap: Snapshot, sink: str, **kw) -> SnapshotSession:
    """kg_search on ``snap`` as the hub's engine reads it for ``sink``: its clearance and audience."""
    hub = hub_session(project, sink)
    return SnapshotSession(SnapshotSearch(snap, hub.clearance, hub.audience, **kw))


def results(answer: dict) -> list[str]:
    return [n["id"] for n in answer["structuredContent"]["results"]]


def test_the_content_hash_of_a_graph_is_the_one_its_build_has(tmp_path, kg_env, monkeypatch):
    """With clusters and a review queue too: the hash names the build without a store."""
    graphs = []
    rows_of_graph = kg_graph_rows.graph_rows

    def kept(graph, **kw):
        graphs.append(graph)
        return rows_of_graph(graph, **kw)

    def clustered(graph, policy, warm=None):
        first, second = sorted(graph.nodes)[:2]
        return Clusters(
            {"c1": {"level": 1, "parent": None, "label": "demo", "size": 2, "kinds": {"File": 2}}},
            {first: ("c1", None), second: ("c1", None)},
            {(1, "c1", "c2"): 0.5},
        )

    monkeypatch.setattr(kg_graph_rows, "graph_rows", kept)
    monkeypatch.setattr(kg_clusters, "available", lambda: True)
    monkeypatch.setattr(kg_clusters, "compute", clustered)
    monkeypatch.setattr(kg_build.kg_clusters, "coverage", lambda found: {"computed": True})
    violation = Violation("missing_prop", "app:file:app/search.py\x1fowner", ("app:file:app/search.py",), (), {})
    monkeypatch.setattr(kg_build.kg_review, "compute", lambda graph: review_items([violation]))
    project = make_searchable(tmp_path, kg_env)
    report = sync_build(project)
    snap = snapshot_of(project)
    graph = graphs[-1]
    assert graph.clusters is not None and graph.review, "the build has clusters and review items"
    assert content.content_hash(graph) == report.content_hash == snap.content_hash
    assert Snapshot.from_graph(graph, snap.build).content_hash == report.content_hash


def test_the_ranking_is_the_stores(tmp_path, kg_env):
    _, snap = built(tmp_path, kg_env)
    ranker = Ranker(snap.view(frozenset(), None, kg_settings.DEFAULT))
    for query in QUERIES:
        assert ranker.search(query, limit=20) == RANKING[query], query
        scored = ranker.search_scored(query, limit=20)
        assert [nid for nid, _ in scored] == RANKING[query], query
        for (nid, score), (want, expected) in zip(scored, TOP_SCORES[query], strict=False):
            assert nid == want, query
            assert score is None if expected is None else score == pytest.approx(expected, abs=1e-5), (query, nid)


def test_kg_search_answers_as_a_session_does_for_each_clearance(tmp_path, kg_env):
    """The app's code is customer: the agent sink sees none of it, the wide one all. One query of many words differs
    by the arrangement locate gave a graph file's search (``evo_agents.kg.locate``), which a snapshot does not read."""
    project, snap = built(tmp_path, kg_env, app_level="customer")
    for sink in ("agent", "wide"):
        mine = reader(project, snap, sink)
        for query in QUERIES[:3] + QUERIES[4:]:
            args = {"query": query, "limit": 10}
            assert results(mine.call("kg_search", args)) == KG_SEARCH[sink][query], (sink, query)
    shown = results(reader(project, snap, "agent").call("kg_search", {"query": "search"}))
    assert shown and not any(nid.startswith(("app:", "symbol:app:")) for nid in shown)


def test_a_bench_snapshot_walks_with_the_projects_settings(tmp_path, kg_env):
    """The walk and entity_code are project settings: the bench's snapshot reads them, as the hub does, or it ranks as an
    unwalked graph."""
    from evo_agents.kg.bench.rows_file import RowsSession
    from evo_agents.kg.bench.run import snapshot_session

    project = entity_project(tmp_path, kg_env)
    walk_on(project)
    kg_settings.write(project.root, kg_settings.of({"entity_code": True}, kg_settings.read(project.root)))
    sync_build(project)
    session = hub_session(project, "agent")
    mine = snapshot_session(RowsSession(project, "agent", snapshot_of(project)))
    for query in ("zephyr launcher", "Quasar", "comet tail", "engine"):
        args = {"query": query, "limit": 10}
        assert results(mine.call("kg_search", args)) == results(session.call("kg_search", args)) == BENCH[query], query


def test_kg_search_reads_the_documents_an_entity_stands_for_and_the_code(tmp_path, kg_env):
    project, snap = built(tmp_path, kg_env)
    mine = reader(project, snap, "agent")
    for query in ("search", "GraphBuilder", "KB-01"):
        ours = mine.call("kg_search", {"query": query})["structuredContent"]
        assert ours["mentioned_by"] == PARTS[query]["mentioned_by"], query
        assert ours["entities"] == PARTS[query]["entities"], query
        assert ours["code"] == PARTS[query]["code"], query
    assert mine.call("kg_node", {"id": "x"})["isError"]


def test_the_edges_of_a_node_come_in_the_stores_order_within_the_ceiling(tmp_path, kg_env):
    _, snap = built(tmp_path, kg_env, app_level="customer")
    nid = "app:file:app/search.py"
    full = snap.view(frozenset(), None, kg_settings.DEFAULT)
    for direction in ("both", "in", "out"):
        assert [e["id"] for e in full.edges_of(nid, direction=direction)] == EDGES[direction], direction
    internal = snap.view(frozenset(), (1, 0), kg_settings.DEFAULT)
    assert full.edges_of(nid) and all(e["label"][0] <= 1 for e in internal.edges_of(nid))
    assert len(internal.edges_of(nid)) < len(full.edges_of(nid))
    assert full.edges_of(nid, limit=1) == full.edges_of(nid)[:1]


def test_the_snapshot_holds_every_live_node_and_edge(tmp_path, kg_env):
    project, snap = built(tmp_path, kg_env)
    rows = rows_of(project)
    assert len(snap.nodes) == len(rows.nodes) and len(snap.edges) == len(rows.edges)
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


def test_the_nearest_vectors_of_a_snapshot_rank_as_their_dot_products_do():
    """A search by meaning on a snapshot ranks its rows by their dot product with the question's vector, a node at its
    best row, of the kinds asked and the audiences readable: on one matrix product, where a product of Python floats
    per row took seconds a question on a graph of tens of thousands of vectors."""
    import random
    from concurrent.futures import ThreadPoolExecutor

    from evo_agents.kg.snapshot_text import Vectors

    rng = random.Random(7)
    rows = tuple(tuple(rng.uniform(-1, 1) for _ in range(8)) for _ in range(60))
    node_ids = tuple(f"lumen-docs:n{i % 40}" for i in range(60))
    kinds = tuple("file" if i % 3 else "symbol" for i in range(60))
    auds = tuple("staff" if i % 7 == 0 else None for i in range(60))
    vectors = Vectors(None, node_ids, kinds, auds, rows)  # type: ignore[arg-type]
    q = [rng.uniform(-1, 1) for _ in range(8)]

    def expected(limit, wanted, readable):
        scores = [sum(a * b for a, b in zip(row, q, strict=True)) for row in rows]
        out = {}
        for i in sorted(range(len(rows)), key=lambda i: -scores[i]):
            if len(out) >= limit:
                break
            if node_ids[i] in out or (wanted and kinds[i] not in wanted):
                continue
            if auds[i] is not None and not readable(auds[i]):
                continue
            out[node_ids[i]] = scores[i]
        return out

    for limit, wanted, readable in (
        (10, (), lambda a: True),
        (5, ("file",), lambda a: False),
        (100, (), lambda a: False),
    ):
        found = vectors.nearest(q, limit, wanted, readable)
        want = expected(limit, wanted, readable)
        assert [n for n, _ in found] == list(want)
        assert all(abs(score - want[n]) < 1e-5 for n, score in found)
    assert vectors.rows == ()  # the matrix took their place
    fresh = Vectors(None, node_ids, kinds, auds, rows)  # type: ignore[arg-type]
    with ThreadPoolExecutor(8) as pool:  # first searches at once: each reads the one matrix made
        firsts = list(pool.map(lambda _: fresh.nearest(q, 10, (), lambda a: True), range(8)))
    assert all([n for n, _ in found] == list(expected(10, (), lambda a: True)) for found in firsts)
    assert Vectors(None, (), (), (), ()).nearest(q, 5, (), lambda a: True) == []  # type: ignore[arg-type]
