"""Fixes of the 2026-11-04 review of kg/: labels of entity props, the ceiling of failed calls, the state a build read,
the memo committed as it goes, Vietnamese names, kg_status under the sink's clearance, the MCP server against messages
it cannot read, and the label filter before the limit."""

import io
import json
import sqlite3
import sys

import pytest
import yaml

from evo_agents.kg import entities, memo, serve, strategy
from evo_agents.kg.build import build_project, verify_build
from evo_agents.kg.corpus import Corpus
from evo_agents.kg.memo import Memo
from evo_agents.kg.policy import Label
from evo_agents.kg.project import load_project_at
from evo_agents.kg.serve import Session
from evo_agents.kg.store import Graph, Store, edge_id
from evo_agents.kg.store_filters import label_columns
from evo_agents.kg.sync import sync_project
from tests.kg.conftest import doc
from tests.kg.projects import write
from tests.kg.test_extract_entities import setup

# Each item's text names who it is from: the model quotes it as the evidence of the one entity both items state.
SAME_ENTITY = """import json, sys
req = json.load(sys.stdin)
said = next(line for line in req["text"].splitlines() if line.startswith("said"))
print(json.dumps({"nodes": [{"kind": "Concept", "name": "Shared", "evidence": said}], "edges": []}))
"""

VIETNAMESE = """import json, sys
json.load(sys.stdin)
print(json.dumps({
    "nodes": [{"kind": "Concept", "name": "Lê Văn Tâm", "evidence": "a"},
              {"kind": "Concept", "name": "Lý Văn Tâm", "evidence": "b"}],
    "edges": [{"src": "Lê Văn Tâm", "rel": "depends_on", "dst": "Lý Văn Tâm", "evidence": "c"}],
}))
"""


def with_entities(project, kg_env, script_text, tmp_path):
    """``project`` with the entities step on, its model the script ``script_text``, synced."""
    script = tmp_path / "model.py"
    script.write_text(script_text, encoding="utf-8")
    mapping = {
        "model": "fake-1",
        "command": [sys.executable, str(script)],
        "node_kinds": ["Concept"],
        "relations": ["depends_on"],
        "items": ["doc"],
    }
    data = {
        "version": 1,
        "name": "ents",
        "knowledge": {"ontology": {"node_kinds": ["Concept"]}},
        "settings": {"entities": mapping},
        "steps": ["entities"],
    }
    chosen = project.harness.root / "strategies" / "ents.yaml"
    write(chosen, yaml.safe_dump(data))
    strategy.apply(strategy.load_file(chosen), project)
    project = load_project_at(project.harness.root, kg_env)
    assert all(r.ok for r in sync_project(project))
    return project


def latest(project):
    store = Store.open_file(project.root / "graph.sqlite")
    return store, store.latest_ready()


def test_entity_label_props_come_from_a_derivation_the_label_reads(fake_project, kg_env, tmp_path):
    project, fake = fake_project
    fake.set(
        items=[
            {**doc("a", "# A\n\nsaid in the customer item\n"), "label": {"level": "customer"}},
            doc("b", "# B\n\nsaid in the internal item\n"),
        ]
    )
    project = with_entities(project, kg_env, SAME_ENTITY, tmp_path)
    report = build_project(project)
    assert report.ok, report.errors
    store, b = latest(project)
    try:
        node = store.node("entity:Concept:shared", b)
    finally:
        store.close()
    assert node["label"][0] == 1, "the internal item states it too: the node is internal"
    assert node["props"]["evidence"] == "said in the internal item"
    assert node["props"]["item"] == "docs:doc:b"


def test_failed_call_ceiling_counts_failed_and_unreadable_calls(tmp_path, kg_env):
    failing = [sys.executable, "-c", "import sys; sys.exit(3)"]
    project, _ = setup(tmp_path, kg_env, extra={"command": failing, "max_calls": 1})
    cov = build_project(project).coverage["entities"]
    assert cov["calls"] == 1 and cov["failed"] == 1 and cov["skipped"] == 2 and "max_calls 1" in cov["stopped"]
    assert cov["tokens_in"] > 0, "a call that reported no usage is estimated"

    garbled = [sys.executable, "-c", "import sys; sys.stdin.read(); print('no json here')"]
    project, _ = setup(tmp_path / "b", kg_env, extra={"command": garbled, "max_tokens": 1})
    cov = build_project(project).coverage["entities"]
    assert cov["calls"] == 1 and cov["failed"] == 1 and "max_tokens 1" in cov["stopped"]


def test_failed_call_ceiling_takes_the_usage_an_unreadable_answer_reported(monkeypatch):
    class Usage:
        prompt_tokens, completion_tokens = 700, 300

    class Answer:
        usage = Usage()
        choices = [type("Choice", (), {"message": type("Message", (), {"content": "not json"})()})()]

    class Client:
        chat = type("Chat", (), {"completions": type("Completions", (), {"create": lambda self, **_: Answer()})()})()

    monkeypatch.setattr(entities, "CLIENT_FACTORY", lambda config: Client())
    with pytest.raises(entities.EntitiesError) as found:
        entities.call_endpoint({"model": "m", "url": "http://localhost:1"}, "text", ["Concept"], [])
    assert found.value.usage == {"input_tokens": 700, "output_tokens": 300}


def test_digest_read_is_the_state_the_build_read(fake_project, monkeypatch):
    project, fake = fake_project
    fake.set(items=[doc("a")])
    assert all(r.ok for r in sync_project(project))
    read = Corpus.read_state

    def read_then_sync(corpus):
        records = read(corpus)
        fake.set(items=[doc("a"), doc("b")])  # a sync lands while the build runs
        assert all(r.ok for r in sync_project(project))
        return records

    monkeypatch.setattr(Corpus, "read_state", read_then_sync)
    report = build_project(project)
    monkeypatch.setattr(Corpus, "read_state", read)
    assert report.ok and report.seconds > 0
    store, b = latest(project)
    try:
        recorded = store.build_row(b)["state_digest"]
    finally:
        store.close()
    corpus = project.corpus()
    try:
        now = corpus.state_digest()
    finally:
        corpus.close()
    assert recorded != now, "the build read one item: it records that state, so the next build is not skipped"
    assert not build_project(project).skipped


def test_memo_commit_as_it_goes_keeps_rows_after_a_crash(tmp_path, monkeypatch):
    path = tmp_path / "memo.sqlite"
    monkeypatch.setattr(memo, "COMMIT_ROWS", 2)
    m = Memo(path)
    for i in range(3):
        m.run("stage", "1", {"i": str(i)}, lambda i=i: {"out": i})
    m.run("paid", "1", {"i": "x"}, lambda: {"answer": 1}, commit=True)
    other = sqlite3.connect(path, timeout=0)  # another build: no write lock left held
    try:
        other.execute("BEGIN IMMEDIATE")
        other.rollback()
        assert other.execute("SELECT count(*) FROM memo").fetchone()[0] == 4
    finally:
        other.close()
    m.db.close()  # a crash: nothing committed at the end


def test_verify_build_closes_what_it_opened(tmp_path, kg_env):
    project, _ = setup(tmp_path, kg_env)
    report = build_project(project)
    opened, closed = [], []
    real_init, real_close = Memo.__init__, Memo.close

    def init(self, path):
        opened.append(self)
        real_init(self, path)

    def close(self):
        closed.append(self)
        real_close(self)

    Memo.__init__, Memo.close = init, close
    try:
        assert verify_build(project, report.content_hash, cold=True)["match"]
    finally:
        Memo.__init__, Memo.close = real_init, real_close
    assert len(opened) == 2 and set(map(id, closed)) == set(map(id, opened))


def test_vietnamese_slug_keeps_names_apart(tmp_path, kg_env, fake_project):
    assert entities.slug("Lê Văn Tâm") == "le-van-tam" != entities.slug("Lý Văn Tâm") == "ly-van-tam"
    assert entities.slug("Đặng Thị Đào") == "dang-thi-dao"
    project, fake = fake_project
    fake.set(items=[doc("a")])
    project = with_entities(project, kg_env, VIETNAMESE, tmp_path)
    assert build_project(project).ok
    store, b = latest(project)
    try:
        edges = [e for e in store.edges_of("entity:Concept:le-van-tam", b, "out") if e["rel"] == "depends_on"]
    finally:
        store.close()
    assert [e["dst"] for e in edges] == ["entity:Concept:ly-van-tam"]


def test_status_clearance_withholds_and_raises_the_label(fake_project, kg_env):
    project, fake = fake_project
    path = project.knowledge_path
    data = yaml.safe_load(path.read_text())
    secret = {**data["sources"][0], "id": "secret", "label": {"level": "customer", "integrity": "U"}}
    data["sources"].append(secret)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    project = load_project_at(project.harness.root, kg_env)
    fake.set(items=[doc("a", "# A\n\nnamed only in the secret source\n")])
    assert all(r.ok for r in sync_project(project))
    assert build_project(project).ok

    status = Session(project, "test-session").call("kg_status", {})
    data = status["structuredContent"]
    shown = {s["id"]: s for s in data["sources"]}
    assert shown["secret"] == {"id": "secret", "withheld": True} and "items" in shown["docs"]
    assert data["label"]["level"] == "internal", "what it showed raises the session's label"
    assert "coverage" not in data["graph"] and data["graph"]["nodes"] > 0
    assert "withheld: above the clearance of this sink" in status["content"][0]["text"]


def run_stdio(monkeypatch, capsys, session, lines):
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(lines) + "\n"))
    assert serve.serve_stdio(session=session) == 0
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_mcp_malformed_messages_get_errors_and_the_server_goes_on(fake_project, monkeypatch, capsys):
    project, fake = fake_project
    fake.set(items=[doc("a")])
    assert all(r.ok for r in sync_project(project))
    assert build_project(project).ok

    def broken(*args, **kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    session = Session(project, "test-session")
    call = '{"jsonrpc": "2.0", "id": %d, "method": "tools/call", "params": %s}'
    replies = run_stdio(
        monkeypatch,
        capsys,
        session,
        [
            "[1, 2]",
            "7",
            "9" * 5000,
            call % (1, '{"name": "kg_search", "arguments": {"query": "a", "limit": 1e999}}'),
            call % (2, '{"name": "kg_search", "arguments": [1]}'),
            '{"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": [1]}',
            call % (4, '{"name": "kg_status", "arguments": {}}'),
            '{"jsonrpc": "2.0", "id": 5, "method": "ping"}',
        ],
    )
    codes = [r.get("error", {}).get("code") for r in replies]
    assert codes[:3] == [-32600, -32600, -32700]
    assert replies[3]["result"]["isError"] and "bad arguments" in replies[3]["result"]["content"][0]["text"]
    assert codes[4:6] == [-32602, -32602]
    assert replies[7] == {"jsonrpc": "2.0", "id": 5, "result": {}}

    monkeypatch.setattr(Store, "search_scored", broken)
    got = session.call("kg_search", {"query": "a"})
    assert got["isError"] and "could not be read" in got["content"][0]["text"]

    class Exploding:
        def call(self, name, args):
            raise RuntimeError("the hub went away")

    replies = run_stdio(
        monkeypatch, capsys, Exploding(), [call % (1, '{"name": "kg_status"}'), '{"id": 2, "method": "ping"}']
    )
    assert replies[0]["error"]["code"] == -32603 and replies[1]["result"] == {}


def test_filter_before_limit_in_search(fake_project, kg_env):
    project, fake = fake_project
    hidden = [
        {**doc(f"h{i:02d}", f"# H{i}\n\nzebra zebra zebra zebra\n"), "label": {"level": "customer"}} for i in range(60)
    ]
    shown = [doc(f"s{i}", f"# S{i}\n\nzebra and many other words of a longer text {i}\n") for i in range(3)]
    fake.set(items=hidden + shown)
    assert all(r.ok for r in sync_project(project))
    assert build_project(project).ok
    for args in ({"query": "zebra", "limit": 3}, {"query": "zebra", "limit": 3, "kinds": ["Section"]}):
        found = Session(project, "test-session").call("kg_search", args)["structuredContent"]["results"]
        assert sorted(n["id"] for n in found) == [f"docs:doc:s{i}#s{i}" for i in range(3)], args


def test_filter_before_limit_in_edges_of(tmp_path):
    low, high = Label(0, 0, "T", frozenset({"p"})), Label(2, 0, "T", frozenset({"p"}))
    graph = Graph()
    unit = {"item_id": "x", "rev": "1", "source": "s", "anchor": "", "hash": None, "span": None}
    graph.units["u:low"] = {**unit, **label_columns(low)}
    graph.units["u:high"] = {**unit, "item_id": "y", **label_columns(high)}
    node = {"kind": "Concept", "props": {}, "status": "parsed", "conf": 1.0, "aliases": []}
    graph.nodes["hub"] = {**node, "name": "hub", "label": low}
    graph.derivations["hub"] = [{"units": ["u:low"], "needs": []}]
    for i in range(10):
        nid, label, u = (f"a{i}", high, "u:high") if i < 8 else (f"z{i}", low, "u:low")
        graph.nodes[nid] = {**node, "name": nid, "label": label}
        graph.derivations[nid] = [{"units": [u], "needs": []}]
        eid = edge_id("hub", "relates_to", nid)
        graph.edges[eid] = {"src": "hub", "rel": "relates_to", "dst": nid, "props": {}, "status": "parsed"}
        graph.edges[eid] |= {"conf": 1.0, "label": label}
        graph.derivations[eid] = [{"units": [u], "needs": ["hub", nid]}]
    store = Store(tmp_path / "graph.sqlite")
    try:
        b = store.write(graph, state_digest="x", coverage={}, stats={}, policy=None)
        store.ceiling = (0, 0)
        assert [e["dst"] for e in store.edges_of("hub", b, "out", limit=2)] == ["z8", "z9"]
        store.ceiling = None
        assert len(store.edges_of("hub", b, "out", limit=2)) == 2
    finally:
        store.close()


def test_harness_walk_leaves_out_symlinks_out_of_the_repo(tmp_path):
    from evo_agents.kg.connectors.harness import walk

    root, outside = tmp_path / "harness", tmp_path / "secret.md"
    write(root / "docs/a.md", "# A\n")
    outside.write_text("# not of the repo\n")
    (root / "docs/link.md").symlink_to(outside)
    (root / "docs/inner.md").symlink_to(root / "docs/a.md")
    assert [rel for _, rel in walk(root)] == ["docs/a.md", "docs/inner.md"]
