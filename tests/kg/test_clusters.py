"""Clusters of a build (``evo_agents.kg.clusters``): the projection, two levels, labels, stable ids, in a build."""

import random

import pytest

from evo_agents.kg import clusters as kg_clusters
from evo_agents.kg.graph_model import Graph, edge_id
from evo_agents.schema.policy import Label, Policy
from tests.kg import built
from tests.kg.built import sync_build  # imported from here by tests.kg.test_review
from tests.kg.hub_graph import hub_status
from tests.kg.projects import commit, make_project, write

needs_engine = pytest.mark.skipif(not kg_clusters.available(), reason="graspologic-native is not installed")

POLICY = Policy(
    "demo",
    {
        "policy": {
            "levels": ["public", "internal", "customer"],
            "sinks": [
                {"id": "hub", "kind": "hub", "clearance": {"level": "internal"}},
            ],
        }
    },
)
INTERNAL = Label(1, 0, "U", frozenset({"demo"}))
CUSTOMER = Label(2, 0, "U", frozenset({"demo"}))
CHAT = Label(1, 0, "U", frozenset({"demo"}), frozenset({"chat:1"}))
TOPICS = [f"topic{i:02d}" for i in range(30)]


def node(graph: Graph, nid: str, kind: str, name: str, label: Label = INTERNAL, **props) -> None:
    graph.nodes[nid] = {"kind": kind, "name": name, "props": props, "status": "parsed", "conf": 1.0, "label": label}


def link(graph: Graph, src: str, rel: str, dst: str, label: Label = INTERNAL) -> None:
    graph.edges[edge_id(src, rel, dst)] = {
        "src": src,
        "rel": rel,
        "dst": dst,
        "props": {},
        "status": "parsed",
        "conf": 1.0,
        "label": label,
    }


def communities(count: int = 30, size: int = 8, seed: int = 3) -> Graph:
    """``count`` groups of ``size`` files, dense inside, a few edges between: file ``f<group>_<i>`` under
    ``<topic>/``, so each group has its own word."""
    rng = random.Random(seed)
    graph = Graph()
    for g in range(count):
        for i in range(size):
            node(graph, f"f{g:02d}_{i}", "File", f"{TOPICS[g]}_{i}.py", path=f"{TOPICS[g]}/part{i}.py")
        for i in range(size):
            for j in range(i + 1, size):
                if rng.random() < 0.7:
                    link(graph, f"f{g:02d}_{i}", "imports", f"f{g:02d}_{j}")
    for _ in range(count):
        a, b = rng.sample(range(count), 2)
        link(graph, f"f{a:02d}_0", "imports", f"f{b:02d}_1")
    return graph


def test_cluster_projection_folds_symbols_and_sections_and_leaves_noise_out():
    graph = Graph()
    node(graph, "app:a.py", "File", "a.py", path="a.py")
    node(graph, "app:b.py", "File", "b.py", path="b.py")
    node(graph, "app:a.py::f", "Symbol", "f", file="app:a.py")
    node(graph, "app:b.py::g", "Symbol", "g", file="app:b.py")
    node(graph, "doc:guide.md", "Document", "guide", path="docs/guide.md")
    node(graph, "doc:guide.md#usage", "Section", "Usage", item="doc:guide.md")
    node(graph, "src:app", "Source", "app")
    node(graph, "commit:1", "Commit", "1")
    node(graph, "app:logo.png", "File", "logo.png", path="assets/logo.png")
    node(graph, "plan:x", "Plan", "x")
    node(graph, "plan:y", "Plan", "y")
    node(graph, "plan:x#1", "PlanStep", "Merge and release")
    node(graph, "plan:y#1", "PlanStep", "merge  and Release")
    node(graph, "plan:x#2", "PlanStep", "Ranking")
    node(graph, "chat:1", "ChatMessage", "secret talk", CHAT)
    node(graph, "app:c.py", "File", "c.py", CUSTOMER, path="c.py")
    link(graph, "app:a.py", "defines", "app:a.py::f")
    link(graph, "app:a.py::f", "calls", "app:b.py::g")
    link(graph, "app:a.py::f", "calls", "app:b.py::g")  # one edge: the id repeats
    link(graph, "app:b.py::g", "calls", "app:a.py::f")
    link(graph, "doc:guide.md#usage", "mentions", "app:a.py::f")
    link(graph, "doc:guide.md#usage", "part_of", "doc:guide.md")
    link(graph, "app:a.py", "in_source", "src:app")
    link(graph, "commit:1", "changes", "app:a.py")
    link(graph, "doc:guide.md", "mentions", "app:logo.png")
    for plan in ("x", "y"):
        link(graph, f"plan:{plan}", "has_step", f"plan:{plan}#1")
        link(graph, f"plan:{plan}#1", "mentions", "app:b.py")
    link(graph, "plan:x", "has_step", "plan:x#2")
    link(graph, "plan:x#2", "mentions", "app:a.py")
    link(graph, "chat:1", "mentions", "app:a.py")
    link(graph, "app:c.py", "imports", "app:a.py")
    link(graph, "app:b.py", "imports", "app:a.py", CHAT)  # an edge of a narrower audience

    weights = kg_clusters.project(graph, kg_clusters.shared_ceiling(POLICY))
    assert weights == {
        ("app:a.py", "app:b.py"): 2,
        ("app:a.py", "doc:guide.md"): 1,
        ("app:a.py", "plan:x#2"): 1,
        ("plan:x", "plan:x#2"): 1,
    }


def test_cluster_shared_ceiling_is_the_hub_sinks_else_the_lowest_sinks():
    assert kg_clusters.shared_ceiling(POLICY) == (1, 0)
    sinks = [
        {"id": "a", "kind": "agent-session", "clearance": {"level": "customer"}},
        {"id": "b", "kind": "cli", "clearance": {"level": "internal"}},
    ]
    assert kg_clusters.shared_ceiling(Policy("demo", {"policy": {"sinks": sinks}})) == (1, 0)
    assert kg_clusters.shared_ceiling(Policy("demo", {})) == (0, 0)


@needs_engine
def test_cluster_two_levels_labels_sizes_kinds_and_links():
    graph = communities()
    found = kg_clusters.compute(graph, POLICY)
    top = {cid: c for cid, c in found.clusters.items() if c["level"] == 1}
    assert len(top) >= 20
    projected = {n for pair in kg_clusters.project(graph, kg_clusters.shared_ceiling(POLICY)) for n in pair}
    assert set(found.members) == projected  # a node with no edge in the projection is in no cluster
    for cid, c in top.items():
        members = [nid for nid, (t, _) in found.members.items() if t == cid]
        assert c["size"] == len(members) and c["kinds"] == {"File": len(members)} and c["parent"] is None
        words = c["label"].split(", ")
        assert 1 <= len(words) <= kg_clusters.LABEL_WORDS
    # A cluster of one group is named by that group's word first.
    group = {nid for nid in graph.nodes if nid.startswith("f05_")}
    named = [c for cid, c in top.items() if group <= {n for n, (t, _) in found.members.items() if t == cid}]
    assert named and named[0]["label"].split(", ")[0] == "topic05"
    assert found.links and all(level == 1 and a < b and w > 0 for (level, a, b), w in found.links.items())
    assert kg_clusters.compute(graph, POLICY) == found  # the same graph and inputs, the same clusters


@needs_engine
def test_cluster_second_level_splits_a_large_cluster(monkeypatch):
    monkeypatch.setattr(kg_clusters, "MAX_CLUSTER_SIZE", 10)
    graph = communities(count=3, size=24)
    found = kg_clusters.compute(graph, POLICY)
    subs = {cid: c for cid, c in found.clusters.items() if c["level"] == 2}
    assert subs
    for cid, c in subs.items():
        members = [nid for nid, (_, s) in found.members.items() if s == cid]
        assert c["size"] == len(members) and {found.members[nid][0] for nid in members} == {c["parent"]}
        assert found.clusters[c["parent"]]["level"] == 1


@needs_engine
def test_cluster_ids_survive_a_changed_file():
    graph = communities()
    before = kg_clusters.compute(graph, POLICY)
    # One file moves: it loses its edges and gains two into another group.
    for eid in [eid for eid, e in graph.edges.items() if "f07_3" in (e["src"], e["dst"])]:
        del graph.edges[eid]
    link(graph, "f07_3", "imports", "f12_2")
    link(graph, "f07_3", "imports", "f12_4")
    after = kg_clusters.compute(graph, POLICY, before)
    old = {cid for cid, c in before.clusters.items() if c["level"] == 1}
    kept = old & {cid for cid, c in after.clusters.items() if c["level"] == 1}
    assert len(kept) >= 0.9 * len(old)


@needs_engine
def test_cluster_members_and_labels_hold_only_what_every_reader_reads():
    graph = communities(count=4, size=6)
    node(graph, "chat:9", "ChatMessage", "zebrasecret", CHAT)
    node(graph, "app:hidden.py", "File", "zebrahidden.py", CUSTOMER, path="zebrahidden.py")
    for i in range(6):
        link(graph, "chat:9", "mentions", f"f01_{i}")
        link(graph, "app:hidden.py", "imports", f"f01_{i}")
    found = kg_clusters.compute(graph, POLICY)
    assert "chat:9" not in found.members and "app:hidden.py" not in found.members
    assert not any("zebra" in c["label"] for c in found.clusters.values())


@needs_engine
def test_cluster_hubs_join_the_cluster_they_reach_most(monkeypatch):
    monkeypatch.setattr(kg_clusters, "HUB_SHARE", 0.05)
    graph = communities(count=10, size=6)
    node(graph, "doc:history.md", "Document", "history", path="docs/history.md")
    for g in range(10):
        link(graph, "doc:history.md", "mentions", f"f{g:02d}_0")
    for i in range(1, 6):
        link(graph, "doc:history.md", "mentions", f"f03_{i}")
    found = kg_clusters.compute(graph, POLICY)
    assert found.members["doc:history.md"][0] == found.members["f03_2"][0]


# -- builds ----------------------------------------------------------------------------------------------------------


@needs_engine
def test_cluster_build_writes_clusters_with_a_stable_content_hash(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    report = sync_build(project, verify=True)
    assert report.verify["match"], report.verify
    clusters = report.coverage["clusters"]
    assert clusters["computed"] and clusters["clusters"] >= 1 and clusters["warm"] is False
    store = built.view_of(project)
    found = store.clusters()
    assert found is not None and found.members
    assert set(found.members) <= set(store.alive_ids("nodes"))
    first = report.content_hash
    store.close()

    again = built.build(project, force=True, verify=True)  # the same corpus, started from its own clusters
    assert again.ok and again.verify["match"] and again.coverage["clusters"]["warm"] is True
    assert again.content_hash == first

    app = project.harness.root.parent / "demo-app"
    write(app / "app/extra.py", "from app.search import search\n\n\ndef more(q):\n    return search(q)\n")
    commit(app, "extra")
    changed = sync_build(project, verify=True)
    assert changed.verify["match"] and changed.content_hash != first
    store = built.view_of(project)
    old = {cid for cid, c in found.clusters.items() if c["level"] == 1}
    now = store.clusters()
    assert len(old & set(now.clusters)) >= 0.9 * len(old)
    text = hub_status(project)["content"][0]["text"]
    assert f"clusters: {changed.coverage['clusters']['clusters']} clusters" in text


def test_cluster_build_without_the_extra_has_none_and_status_says_so(tmp_path, kg_env, monkeypatch):
    monkeypatch.setattr(kg_clusters, "available", lambda: False)
    project = make_project(tmp_path, kg_env)
    report = sync_build(project, verify=True)
    assert report.verify["match"] and report.coverage["clusters"]["computed"] is False
    store = built.view_of(project)
    assert store.clusters() is None
    store.close()
    text = hub_status(project)["content"][0]["text"]
    assert "clusters: none, graspologic-native is not installed: pip install 'evo-ak[clusters]'" in text


@needs_engine
def test_cluster_inputs_change_the_build_inputs(tmp_path, kg_env, monkeypatch):
    from evo_agents.kg.build import build_inputs

    project = make_project(tmp_path, kg_env)
    corpus = project.corpus()
    try:
        first = build_inputs(project, corpus)
        monkeypatch.setattr(kg_clusters, "PARAMS", {**kg_clusters.PARAMS, "resolution": 2.0})
        assert build_inputs(project, corpus) != first
    finally:
        corpus.close()
