"""The edge model M3 (evo_agents.kg.links): split, false edges, threshold, training, candidates, proposals, the CLI
and the hub route, which only proposes."""

import json
import sqlite3

import pytest

from evo_agents.cli import main
from evo_agents.kg import links
from evo_agents.kg.bench import compare, links_eval
from tests.kg.projects import make_project
from tests.kg.test_store import sync_build


def toy_graph(groups: int = 6, size: int = 6) -> links.LinkGraph:
    """Files in groups: each imports every other file of its group; one Symbol per file, defined by it; two Symbols
    share the name ``run`` per group pair, so names are ambiguous."""
    g = links.LinkGraph()
    for k in range(groups):
        for i in range(size):
            g.add_node(f"r:g{k}/f{i}.py", "File", f"f{i}.py", f"g{k}/f{i}.py")
            g.add_node(f"r:g{k}/f{i}.py::s{k}_{i}", "Symbol", "run" if i == 0 else f"s{k}_{i}", None)
    for k in range(groups):
        for i in range(size):
            src = f"r:g{k}/f{i}.py"
            g.add_edge(src, "defines", f"{src}::s{k}_{i}", "parsed")
            for j in range(size):
                if i != j:
                    g.add_edge(src, "imports", f"r:g{k}/f{j}.py", "parsed")
    return g


def test_threshold_for_is_the_lowest_that_reaches_the_precision():
    np = pytest.importorskip("numpy")
    pos = np.array([0.9, 0.8, 0.6, 0.3])
    neg = np.array([0.7, 0.2, 0.1])
    assert links.threshold_for(pos, neg, 1.0) == 0.8
    assert links.threshold_for(pos, neg, 0.85) == 0.8  # at 0.6: 3 true of 4
    assert links.threshold_for(pos, neg, 0.8) == 0.3  # 4 true of 5 at or above 0.3
    assert links.threshold_for(np.array([0.1]), np.array([0.9]), 0.8) is None
    row = links.at(pos, neg, 0.6)
    assert (row["precision"], row["recall"], row["proposed"], row["true"]) == (0.75, 0.75, 4, 3)
    assert links.at(pos, neg, None)["proposed"] == 0


def test_split_hides_a_tenth_and_keeps_every_node_in_training():
    np = pytest.importorskip("numpy")
    g = toy_graph()
    train, chosen, test = links.split(g, seed=3)
    assert len(train) + len(chosen) + len(test) == len(g.parsed)
    assert len(test) == int(len(g.parsed) * links.HIDE)
    assert len(chosen) == int(len(g.parsed) * links.CHOOSE)
    seen = set(train[:, 0].tolist()) | set(train[:, 2].tolist())
    assert {int(x) for x in np.concatenate([test[:, [0, 2]].ravel(), chosen[:, [0, 2]].ravel()])} <= seen


def test_false_edges_take_a_twin_of_the_same_name_when_one_exists():
    pytest.importorskip("numpy")
    g = toy_graph()
    defines = g.rel_index["defines"]
    edges = [e for e in g.parsed if e[1] == defines and g.names[e[2]] == "run"]
    fake, same = links.false_edges(g, edges, seed=1)
    assert same == len(edges) == len(fake)
    for (h, r, t), (fh, fr, ft) in zip(edges, fake.tolist(), strict=True):
        assert (fh, fr) == (h, r) and ft != t and g.names[ft] == "run"
        assert ft not in g.known[(h, r)]


@pytest.mark.parametrize("kind", links.MODELS)
def test_the_model_learns_hidden_edges_of_a_toy_graph(kind):
    pytest.importorskip("numpy")
    model, numbers = links.evaluate(toy_graph(), model=kind, dim=16, epochs=60, precision=0.8)
    assert numbers["n"] == int(len(toy_graph().parsed) * links.HIDE)
    assert numbers["auc"] > 0.8, numbers
    assert numbers["fit"]["epochs"] == 60
    assert isinstance(model, links.Model)


def test_candidates_come_from_review_items_resolved_rivals_and_tests():
    g = links.LinkGraph()
    g.add_node("r:docs/guide.md", "Document", "guide.md", "docs/guide.md")
    g.add_node("r:app/a.py", "File", "a.py", "app/a.py")
    g.add_node("r:app/b.py", "File", "b.py", "app/b.py")
    g.add_node("r:tests/test_a.py", "File", "test_a.py", "tests/test_a.py")
    g.add_node("r:app/a.py::load", "Symbol", "load", None)
    g.add_node("r:app/b.py::load", "Symbol", "load", None)
    g.add_edge("r:app/b.py", "calls", "r:app/a.py::load", "resolved")
    review = [
        {"rule": "dangling_mention", "nodes": [{"id": "r:docs/guide.md"}], "detail": {"samples": ["`app/a.py`"]}},
        {"rule": "ambiguous_match", "nodes": [{"id": "r:docs/guide.md"}], "detail": {"samples": ["load()"]}},
        {"rule": "orphan", "nodes": [{"id": "r:docs/guide.md"}], "detail": {"samples": ["app/b.py"]}},
    ]
    found = {(c.src, c.rel, c.dst, c.why) for c in links.candidates(g, review)}
    assert ("r:docs/guide.md", "mentions", "r:app/a.py", "dangling") in found
    assert ("r:docs/guide.md", "mentions", "r:app/a.py::load", "ambiguous") in found
    assert ("r:docs/guide.md", "mentions", "r:app/b.py::load", "ambiguous") in found
    assert ("r:app/b.py", "calls", "r:app/b.py::load", "ambiguous") in found  # the rival of a resolved edge
    assert ("r:app/b.py", "calls", "r:app/a.py::load", "ambiguous") not in found  # the graph has it
    assert ("r:tests/test_a.py", "verifies", "r:app/a.py", "test") in found
    assert not any(c[2] == "r:app/b.py" and c[0] == "r:docs/guide.md" for c in found)  # not a rule it reads


def test_proposals_keep_a_clear_winner_above_the_threshold():
    c = links.Candidate
    scored = [
        (c("d", "mentions", "x1", "ambiguous", "load"), 0.95),
        (c("d", "mentions", "x2", "ambiguous", "load"), 0.40),
        (c("d", "mentions", "y1", "ambiguous", "save"), 0.90),
        (c("d", "mentions", "y2", "ambiguous", "save"), 0.88),  # too close: no proposal for save
        (c("d", "mentions", "z1", "dangling", "z.py"), 0.30),  # under the threshold
        (c("t", "verifies", "w", "test", "w"), 0.85),
    ]
    out = links.proposals(scored, threshold=0.5)
    assert [(p["src"], p["dst"]) for p in out] == [("d", "x1"), ("t", "w")]
    first = out[0]
    assert first["kind"] == "link" and first["rivals"] == 1 and first["probability"] == 0.95
    assert "0.95" in first["reason"] and "'load'" in first["reason"]


def _edges(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute("SELECT count(*), max(tx_from) FROM edges").fetchone()
    finally:
        con.close()


def test_read_and_propose_on_a_built_project_write_nothing(tmp_path, kg_env, capsys):
    pytest.importorskip("numpy")
    project = make_project(tmp_path, kg_env)
    sync_build(project)
    from evo_agents.kg.store import Store

    store = Store.open_readonly(project)
    try:
        g = links.read(store, store.latest_ready())
    finally:
        store.close()
    assert g.parsed and all(len(e) == 3 for e in g.parsed)
    assert set(g.kinds) >= {"File", "Symbol"}

    review = tmp_path / "review.json"
    guide = next(i for i in g.ids if i.endswith("docs/guide.md"))
    review.write_text(
        json.dumps({"items": [{"rule": "dangling_mention", "nodes": [{"id": guide}], "detail": {"samples": ["x.py"]}}]})
    )
    graph = project.root / "graph.sqlite"
    before = _edges(graph)
    out = tmp_path / "proposals.json"
    argv = ["kg", "links", "propose", "--project", str(project.harness.root), "--review", str(review)]
    argv += ["--dim", "8", "--epochs", "3", "--precision", "0", "--out", str(out), "--json"]
    assert main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["evaluation"]["model"] == links.MODELS[0] and result["sent"] == []
    assert json.loads(out.read_text())["candidates"] == result["candidates"]
    assert _edges(graph) == before  # proposals only: the graph is as it was


def test_eval_summary_is_checked_by_bench_show_without_split(tmp_path, kg_env, capsys):
    pytest.importorskip("numpy")
    project = make_project(tmp_path, kg_env)
    sync_build(project)
    out = tmp_path / "m3.json"
    argv = ["kg", "links", "eval", "--set", f"demo={project.harness.root}", "--out", str(out)]
    assert main([*argv, "--dim", "8", "--epochs", "3", "--precision", "0"]) == 0
    summary = json.loads(out.read_text())
    assert summary["split"] == "holdout" and summary["kind"] == "links"
    part = summary["benches"]["demo"]["parts"]["holdout"]
    assert part["primary"] == part["precision"]
    capsys.readouterr()
    assert main(["kg", "bench", "show", str(out), "--precision-min", "0"]) == 0
    assert main(["kg", "bench", "show", str(out), "--precision-min", "1"]) == 1


def test_pooled_sums_true_and_proposed_edges():
    rows = [
        {"n": 10, "true": 6, "proposed": 7, "auc": 0.9, "fit": {"seconds": 1.0}},
        {"n": 30, "true": 10, "proposed": 13, "auc": 0.7, "fit": {"seconds": 2.0}},
    ]
    part = links_eval.pooled(rows)
    assert part["precision"] == 0.8 and part["recall"] == 0.4 and part["n"] == 40 and part["auc"] == 0.8
    summary = links_eval.summary({"x": [("g", rows[0])]}, {"model": "distmult"}, 7, 4)
    assert summary["benches"]["x"]["method"] == "links-distmult"
    checks = compare.check(summary, precision_min=0.8)
    assert [c[0] for c in checks] == ["precision-min"] and all(c[1] for c in checks)


class FakeHub:
    url = "https://hub.example.test"
    token = "t"

    def __init__(self):
        self.rev = 4
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        assert method == "GET", "the hub route only reads; proposals go through kg_propose"
        if path.endswith("/decisions"):
            return {"rev": self.rev}
        return {"items": [{"rule": path.rsplit("rule=", 1)[1], "nodes": [{"id": "r:a"}]}]}


class FakeClient:
    def __init__(self, hub):
        self.hub = hub
        self.sent = []

    def call_tool(self, name, args, meta):
        self.sent.append((name, args, meta))
        if args["to"] == "r:gone":
            self.hub.rev += 1
            return {"isError": True, "content": [{"type": "text", "text": "no node r:gone"}]}
        self.hub.rev += 1
        n = len(self.sent)
        return {"structuredContent": {"decision": {"number": n, "state": "proposed"}, "rev": self.hub.rev}}


def _proposal(dst):
    return {"src": "r:doc", "rel": "mentions", "dst": dst, "reason": "kg links: test"}


def test_send_proposes_each_with_the_revision_and_keeps_refusals():
    from evo_agents.hub import kg_links
    from evo_agents.kg.serve import DATA_META

    hub = FakeHub()
    client = FakeClient(hub)
    out = kg_links.send("demo", [_proposal("r:a"), _proposal("r:gone"), _proposal("r:b")], hub=hub, client=client)
    assert [s.get("number") for s in out] == [1, None, 3]
    assert out[1]["error"] == "no node r:gone"
    assert all(s["state"] == "proposed" for s in (out[0], out[2]))
    names = {name for name, _, _ in client.sent}
    assert names == {"kg_propose"}
    revs = [args["base_rev"] for _, args, _ in client.sent]
    assert revs == [4, 5, 6]  # the answer's revision, then the one read again after a refusal
    assert all(args["kind"] == "link" and args["project"] == "demo" for _, args, _ in client.sent)
    assert all(meta == {DATA_META: True} for _, _, meta in client.sent)


def test_send_dry_run_and_review_only_read():
    from evo_agents.hub import kg_links

    hub = FakeHub()
    client = FakeClient(hub)
    out = kg_links.send("demo", [_proposal("r:a")], dry_run=True, hub=hub, client=client)
    assert out == [{"src": "r:doc", "rel": "mentions", "dst": "r:a", "number": None, "dry_run": True}]
    assert client.sent == []
    items = kg_links.read_review("demo", hub=hub)
    assert [i["rule"] for i in items] == list(kg_links.REVIEW_RULES)
    assert all(method == "GET" for method, _ in hub.calls)
