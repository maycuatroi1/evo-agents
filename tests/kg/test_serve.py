import json
import random
import re
from pathlib import Path

import pytest

from evo_agents.kg import explore, serve
from evo_agents.kg.build import build_project
from evo_agents.kg.explore import Explorer
from evo_agents.kg.extract.languages import CROSS_FILE_CALLS, TABLES, coverage_of
from evo_agents.kg.refs import readable, stored
from evo_agents.kg.serve import Session, diff_lines, handle
from evo_agents.kg.sessions import READ_META, labels_in
from evo_agents.kg.store import Store, edge_id
from evo_agents.kg.sync import sync_project
from tests.kg.projects import commit, make_project, write

RANK = "symbol:app:app/search.py::rank_results"
SEARCH = "symbol:app:app/search.py::search"
TEST_SEARCH = "symbol:app:tests/test_search.py::test_search"
SEARCH_PY = "app:file:app/search.py"
SERVER_PY = "app:file:app/server.py"
HANDLE = "symbol:app:app/server.py::Server.handle"

DIFF = """diff --git a/app/search.py b/app/search.py
index 1111111..2222222 100644
--- a/app/search.py
+++ b/app/search.py
@@ -1,2 +1,2 @@
 def rank_results(items):
-    return sorted(items)
+    return sorted(items, reverse=True)
"""

QUERIES = [
    ("kg_search", {"query": "search"}),
    ("kg_search", {"query": "KB-01"}),
    ("kg_node", {"id": "plan:demo"}),
    ("kg_node", {"id": "symbol:app:app/search.py::search"}),
    ("kg_context", {"query": "search", "budget_tokens": 2000}),
    ("kg_context", {"ids": ["usecase:KB-01"], "hops": 2}),
    ("kg_impact", {"id": RANK}),
    ("kg_impact", {"paths": ["app:app/server.py"], "diff": DIFF}),
    ("kg_impact", {"ids": ["usecase:KB-01", "plan:demo/step:1"], "depth": 3}),
    ("kg_path", {"from": "usecase:KB-01", "to": "seam:search-api"}),
    ("kg_path", {"from": "plan:demo"}),
]


def built(tmp_path, home, **kw):
    project = make_project(tmp_path, home, **kw)
    sync_project(project)
    report = build_project(project)
    assert report.ok, report.errors
    return project


def built_wider(tmp_path, home, **kw):
    """The demo project plus a test that verifies search, a second search.py, and a step scheduling KB-02."""
    project = make_project(tmp_path, home, **kw)
    app = tmp_path / "demo-app"
    test = "from app.search import search\n\n\ndef test_search():\n    assert search('q')\n"
    write(app / "tests/test_search.py", test)
    write(app / "lib/search.py", "def search(query):\n    return query\n")
    commit(app, "tests")
    bindings = project.harness.root / "bindings/core.yaml"
    write(
        bindings,
        bindings.read_text(encoding="utf-8")
        + "  - from: app:tests/test_search.py::test_search\n    rel: verifies\n    to: app:app/search.py::search\n"
        + "  - from: demo#2\n    rel: schedules\n    to: usecase:KB-02\n",
    )
    sync_project(project)
    report = build_project(project)
    assert report.ok, report.errors
    return project


def built_kin(tmp_path, home):
    """The demo project plus a class and its subclasses in another file, and a Swift file."""
    project = make_project(tmp_path, home)
    app = tmp_path / "demo-app"
    write(app / "app/shapes.py", "class Base:\n    def run(self):\n        return 1\n")
    write(
        app / "app/kids.py",
        "from app.shapes import Base\n\n\nclass Child(Base):\n    def run(self):\n        return super().run()\n\n\n"
        "class Plain(Base):\n    def go(self):\n        return self.run()\n\n\n"
        "class Quiet(Base):\n    def run(self):\n        return 0\n",
    )
    write(app / "app/Shape.swift", "class Shape {\n    func area() -> Int {\n        return 1\n    }\n}\n")
    commit(app, "kin")
    sync_project(project)
    report = build_project(project)
    assert report.ok, report.errors
    return project


def add_edge(project, src, rel, dst, status, level=1):
    """Put an edge straight into the latest build: statuses and labels no stage produces here."""
    store = Store.for_project(project)
    with store.db:
        store.db.execute(
            "INSERT INTO edges (edge_id, src, rel, dst, props, status, conf, label_lvl, label_loc, label_int,"
            " content, tx_from) VALUES (?, ?, ?, ?, '{}', ?, 0.5, ?, 0, 'U', 'test', ?)",
            (edge_id(src, rel, dst), src, rel, dst, status, level, store.latest_ready()),
        )
    store.close()


def impacted(result) -> dict[str, tuple]:
    return {r["node"]["id"]: (r["hop"], r["rel"], r["status"]) for r in result["structuredContent"]["impacted"]}


def answers(project, sink="agent"):
    session = Session(project, sink)
    return [json.dumps(session.call(name, args), ensure_ascii=False, sort_keys=True) for name, args in QUERIES]


def test_tools_answer(tmp_path, kg_env):
    project = built(tmp_path, kg_env)
    session = Session(project, "agent")
    found = session.call("kg_search", {"query": "rank_results"})
    assert not found.get("isError")
    assert any(r["id"] == "symbol:app:app/search.py::rank_results" for r in found["structuredContent"]["results"])
    card = session.call("kg_node", {"id": "usecase:KB-01"})
    text = card["content"][0]["text"]
    assert "implements" in text and "app/search.py::search" in text
    ctx = session.call("kg_context", {"query": "search-api"})
    nodes = {n["id"] for n in ctx["structuredContent"]["nodes"]}
    assert "seam:search-api" in nodes and "repo:app" in nodes


def test_no_result_names_an_element_outside_the_visible_set(tmp_path, kg_env):
    # The app repo is customer-level; the agent sink is cleared for internal only.
    project = built(tmp_path, kg_env, app_level="customer")
    store = Store.for_project(project)
    b = store.latest_ready()
    hidden = {
        r[0]
        for r in store.db.execute(
            "SELECT node_id FROM nodes WHERE label_lvl > 1 AND tx_from <= :b AND (tx_to IS NULL OR tx_to > :b)",
            {"b": b},
        )
    }
    assert "symbol:app:app/search.py::search" in hidden
    outputs = answers(project, "agent")
    outputs.append(
        json.dumps(Session(project, "agent").call("kg_node", {"id": "plan:demo/step:2"}), ensure_ascii=False)
    )
    for out in outputs:
        for node_id in hidden:
            assert node_id not in out, (node_id, out[:300])
    wide = " ".join(answers(project, "wide"))
    assert "symbol:app:app/search.py::search" in wide


def test_noninterference_between_projects(tmp_path, kg_env):
    world_a = tmp_path / "a"
    world_b = tmp_path / "b"
    world_a.mkdir()
    world_b.mkdir()
    home_a, home_b = world_a / "home", world_b / "home"

    # World A: project demo alone. World B: demo built next to another project in the same home.
    alone = built(world_a, home_a)
    other = make_project(world_b, home_b, name="other")
    sync_project(other)
    together = make_project(world_b, home_b)
    sync_project(together)
    build_project(other)
    assert build_project(together).ok
    assert answers(alone) == answers(together)


def test_mcp_protocol_and_truncation(tmp_path, kg_env, monkeypatch):
    project = built(tmp_path, kg_env)
    session = Session(project, "agent")
    init = handle(
        session, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    )
    assert init["result"]["protocolVersion"] == "2025-06-18"
    tools = handle(session, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert {t["name"] for t in tools["result"]["tools"]} == {
        "kg_search",
        "kg_context",
        "kg_node",
        "kg_impact",
        "kg_path",
        "kg_status",
        "kg_more",
    }
    assert handle(session, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    unknown = handle(session, {"jsonrpc": "2.0", "id": 3, "method": "nope"})
    assert unknown["error"]["code"] == -32601

    monkeypatch.setattr(serve, "CAP_CHARS", 300)
    first = session.call("kg_node", {"id": "plan:demo"})
    text = first["content"][0]["text"]
    assert "[truncated" in text and len(text) < 400
    handle_id = first["structuredContent"]["handle"]
    rest = session.call("kg_more", {"handle": handle_id})
    assert not rest.get("isError")
    gone = session.call("kg_more", {"handle": "nope"})
    assert gone["isError"]


def test_a_client_gets_the_text_alone_unless_it_asks_for_the_data(tmp_path, kg_env, monkeypatch):
    # Claude Code 2.1.296 shows the model a result's structuredContent instead of its text when it has one: the model
    # would read JSON 2.7 to 3.3 times longer than the text, which CAP_CHARS and the budgets bound.
    project = built(tmp_path, kg_env)
    session = Session(project, "agent")

    def tool(id_: int, name: str, arguments: dict, meta: dict | None = None) -> dict:
        params = {"name": name, "arguments": arguments, **({"_meta": meta} if meta else {})}
        return handle(session, {"jsonrpc": "2.0", "id": id_, "method": "tools/call", "params": params})["result"]

    full = session.call("kg_node", {"id": "plan:demo"})
    shown = tool(1, "kg_node", {"id": "plan:demo"})
    assert shown == {"content": full["content"]}
    data = full["structuredContent"]
    read = {"project": "demo", "label": data["label"], "results_read": labels_in(data)[1]}
    assert shown["content"][0]["_meta"] == {READ_META: read} and read["results_read"] > 0
    assert tool(2, "kg_node", {"id": "plan:demo"}, {serve.DATA_META: True}) == full
    assert tool(3, "kg_node", {"id": "plan:demo"}, {serve.DATA_META: "yes"}) == shown  # only true asks
    status = tool(4, "kg_status", {})
    assert list(status) == ["content"] and "_meta" not in status["content"][0]  # reveals no element
    missing = tool(5, "kg_node", {"id": "nope"})
    assert missing == {"content": missing["content"], "isError": True}

    monkeypatch.setattr(serve, "CAP_CHARS", 300)
    cut = tool(6, "kg_node", {"id": "plan:demo"})
    text = cut["content"][0]["text"]
    assert list(cut) == ["content"] and "[truncated:" in text and len(text) < 400
    assert cut["content"][0]["_meta"] == {READ_META: read}  # counted before the cut
    more = tool(7, "kg_more", {"handle": text.rsplit("handle ", 1)[1].rstrip("]")})
    assert list(more) == ["content"] and "_meta" not in more["content"][0] and more["content"][0]["text"]


def test_eval_mocks_carry_the_real_tool_list():
    # The plugin evals answer from mocks; _tools.json gives the mocked tools their real descriptions and schemas.
    saved = Path(__file__).parents[2] / "plugins" / "evo-kg" / "evals" / "mocks" / "evo-kg" / "_tools.json"
    assert json.loads(saved.read_text(encoding="utf-8")) == {"tools": serve.TOOLS}


def test_unbound_server_explains_how_to_bind(tmp_path, kg_env, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("EVO_KG_PROJECT", raising=False)
    session = serve.make_session(None, "agent")
    result = session.call("kg_search", {"query": "x"})
    assert result["isError"] and "--project" in result["content"][0]["text"]


def test_impact_by_id_walks_back_to_dependents_and_groups_documents(tmp_path, kg_env):
    project = built_wider(tmp_path, kg_env)
    session = Session(project, "agent")
    result = session.call("kg_impact", {"id": RANK})
    got = impacted(result)
    assert got == {
        SEARCH: (1, "calls", "resolved"),
        SEARCH_PY: (1, "defines", "parsed"),
        HANDLE: (2, "calls", "resolved"),  # server.py imports search from app.search and calls it
        SERVER_PY: (2, "imports", "parsed"),
        "app:file:tests/test_search.py": (2, "imports", "parsed"),
        TEST_SEARCH: (2, "verifies", "declared"),
    }
    docs = {d["node"]["id"]: [m["node"] for m in d["mentions"]] for d in result["structuredContent"]["documents"]}
    assert docs["app:file:docs/guide.md#details"] == [RANK]
    assert docs["plan:demo/step:2"] == [SERVER_PY]
    assert not set(docs) & set(got)
    text = result["content"][0]["text"]
    # The text names nodes by their readable ids, and a symbol's name only once, in its id.
    assert "[app:app/search.py::search] Symbol (parsed) lines 5-6" in text
    assert "-calls-> [app:app/search.py::rank_results] (resolved)" in text
    assert "documents to re-check:" in text
    assert set(impacted(session.call("kg_impact", {"id": RANK, "depth": 1}))) == {SEARCH, SEARCH_PY}
    assert session.call("kg_impact", {})["isError"]


def test_impact_by_paths_and_by_diff(tmp_path, kg_env):
    project = built_wider(tmp_path, kg_env)
    session = Session(project, "agent")
    by_path = session.call("kg_impact", {"paths": ["app:app/search.py"]})
    assert [s["id"] for s in by_path["structuredContent"]["seeds"]] == [SEARCH_PY]
    assert set(impacted(by_path)) == {SERVER_PY, "app:file:tests/test_search.py"}

    # The changed line sits in rank_results: the file and that symbol are the seeds, search is not.
    more = "--- a/search.py\n+++ b/search.py\n@@ -1 +1 @@\n-x\n+y\n--- a/nowhere.py\n+++ b/nowhere.py\n"
    by_diff = session.call("kg_impact", {"diff": DIFF + more})["structuredContent"]
    assert [s["id"] for s in by_diff["seeds"]] == [SEARCH_PY, RANK]
    # A bare path names no repo: two files end with it, so both come back instead of a guess.
    assert by_diff["ambiguous"] == [{"path": "search.py", "candidates": [SEARCH_PY, "app:file:lib/search.py"]}]
    assert by_diff["unmatched"] == ["nowhere.py"]
    assert SEARCH in {r["node"]["id"] for r in by_diff["impacted"]}

    in_search = "--- a/app/search.py\n+++ b/app/search.py\n@@ -6 +6 @@\n-    return rank_results([query])\n+    pass\n"
    seeds = session.call("kg_impact", {"diff": in_search})["structuredContent"]["seeds"]
    assert [s["id"] for s in seeds] == [SEARCH_PY, SEARCH]
    # An import added at the top of a file falls in no symbol: the file alone is the seed.
    top = "--- a/app/server.py\n+++ b/app/server.py\n@@ -1,1 +1,2 @@\n from app.search import search\n+import os\n"
    seeds = session.call("kg_impact", {"diff": top})["structuredContent"]["seeds"]
    assert [s["id"] for s in seeds] == [SERVER_PY]


def test_impact_reads_changed_lines_from_a_diff():
    diff = (
        "diff --git a/q.sql b/q.sql\n--- a/q.sql\n+++ b/q.sql\n@@ -3,3 +3,3 @@\n keep\n--- old\n+-- new\n keep\n"
        "diff --git a/img.png b/img.png\nBinary files a/img.png and b/img.png differ\n"
        "--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n"
    )
    assert diff_lines(diff) == {"q.sql": {3, 4}, "img.png": set(), "gone.py": {1, 2}}


def test_impact_follows_no_proposed_edge(tmp_path, kg_env):
    project = built(tmp_path, kg_env)
    # Server.handle calls search for real (resolved by the build); rank_results does not call it.
    add_edge(project, RANK, "calls", SEARCH, "proposed")
    add_edge(project, "symbol:app:app/server.py::Server", "calls", SEARCH, "introspected")
    add_edge(project, "harness:file:CLUSTER.md#demo", "mentions", SEARCH, "proposed")
    result = Session(project, "agent").call("kg_impact", {"id": SEARCH, "depth": 1})
    got = impacted(result)
    assert got["symbol:app:app/server.py::Server"] == (1, "calls", "introspected")
    assert got[HANDLE] == (1, "calls", "resolved")
    assert RANK not in got
    docs = {d["node"]["id"] for d in result["structuredContent"]["documents"]}
    assert "app:file:docs/guide.md#guide" in docs and "harness:file:CLUSTER.md#demo" not in docs


def test_impact_and_path_do_not_expand_hubs(tmp_path, kg_env, monkeypatch):
    project = built(tmp_path, kg_env)
    session = Session(project, "agent")
    assert SERVER_PY in impacted(session.call("kg_impact", {"id": RANK}))
    monkeypatch.setattr(serve, "HUB_DEGREE", 5)  # app/search.py has six edges, its symbols fewer
    result = session.call("kg_impact", {"id": RANK})
    hubs = [r["node"]["id"] for r in result["structuredContent"]["impacted"] if r["hub"]]
    assert hubs == [SEARCH_PY] and "(hub, not expanded)" in result["content"][0]["text"]
    assert SERVER_PY not in impacted(result)
    path = session.call("kg_path", {"from": RANK, "to": SERVER_PY})["structuredContent"]
    assert path["found"] and path["hops"] == 3
    assert SEARCH_PY not in [n["id"] for n in path["nodes"]]


def test_impact_and_path_hide_what_the_sink_cannot_see(tmp_path, kg_env):
    project = built(tmp_path, kg_env, app_level="customer")
    add_edge(project, "harness:file:knowledge.yaml", "mentions", SEARCH_PY, "parsed")
    add_edge(project, "harness:file:harness.yaml", "mentions", SEARCH_PY, "parsed")
    add_edge(project, "harness:file:knowledge.yaml", "depends_on", "harness:file:contracts.yaml", "parsed", level=2)
    agent, wide = Session(project, "agent"), Session(project, "wide")

    assert SEARCH in impacted(wide.call("kg_impact", {"id": "usecase:KB-01"}))
    assert SEARCH not in json.dumps(agent.call("kg_impact", {"id": "usecase:KB-01"}))
    contracts = {"id": "harness:file:contracts.yaml", "depth": 1}
    assert "harness:file:knowledge.yaml" in impacted(wide.call("kg_impact", contracts))
    assert "harness:file:knowledge.yaml" not in impacted(agent.call("kg_impact", contracts))

    # The only path between the two harness files runs through a customer-level file.
    ends = {"from": "harness:file:knowledge.yaml", "to": "harness:file:harness.yaml"}
    assert wide.call("kg_path", ends)["structuredContent"]["hops"] == 2
    blocked = agent.call("kg_path", ends)
    assert blocked["structuredContent"]["found"] is False
    assert SEARCH_PY not in json.dumps(blocked)
    hidden_end = agent.call("kg_path", {"from": "plan:demo", "to": SEARCH_PY})
    assert hidden_end["isError"] and SEARCH_PY not in json.dumps(hidden_end)


def test_path_takes_the_shortest_then_the_strongest(tmp_path, kg_env):
    project = built(tmp_path, kg_env)
    # An equally short path through proposed edges, via a parent that sorts first.
    add_edge(project, "app:file:app/__init__.py", "mentions", RANK, "proposed")
    add_edge(project, SERVER_PY, "imports", "app:file:app/__init__.py", "proposed")
    session = Session(project, "agent")
    result = session.call("kg_path", {"from": RANK, "to": SERVER_PY})
    data = result["structuredContent"]
    assert [n["id"] for n in data["nodes"]] == [RANK, SEARCH_PY, SERVER_PY]
    assert [(e["rel"], e["status"]) for e in data["edges"]] == [("defines", "parsed"), ("imports", "parsed")]
    assert "<-defines- (parsed) [app:app/search.py] File (parsed)" in result["content"][0]["text"]
    add_edge(project, RANK, "mentions", SERVER_PY, "proposed")
    assert session.call("kg_path", {"from": RANK, "to": SERVER_PY})["structuredContent"]["hops"] == 1
    assert session.call("kg_path", {"from": RANK, "to": RANK})["structuredContent"]["hops"] == 0
    assert session.call("kg_path", {"from": RANK, "bogus": 1})["isError"]


def test_path_without_to_traces_chains(tmp_path, kg_env):
    project = built_wider(tmp_path, kg_env)
    session = Session(project, "agent")
    chains = session.call("kg_path", {"from": "usecase:KB-01"})["structuredContent"]
    assert {n["id"]: n["hop"] for n in chains["nodes"]} == {"usecase:KB-01": 0, SEARCH: 1, TEST_SEARCH: 2}
    result = session.call("kg_path", {"from": "plan:demo"})
    edges = {(e["src"], e["rel"], e["dst"]) for e in result["structuredContent"]["edges"]}
    assert ("plan:demo/step:2", "schedules", "usecase:KB-02") in edges
    assert ("plan:demo", "touches", "seam:search-api") in edges
    assert {rel for _, rel, _ in edges} <= set(serve.CHAIN_RELS)
    assert "\n    -schedules-> (declared) [usecase:KB-02]" in result["content"][0]["text"]
    short = session.call("kg_path", {"from": "plan:demo", "max_hops": 1})["structuredContent"]
    assert "usecase:KB-02" not in {n["id"] for n in short["nodes"]}


def test_impact_and_path_truncate_behind_a_handle(tmp_path, kg_env, monkeypatch):
    project = built_wider(tmp_path, kg_env)
    session = Session(project, "agent")
    for name, args in (("kg_impact", {"id": RANK}), ("kg_path", {"from": "plan:demo"})):
        full = session.call(name, args)["content"][0]["text"]
        monkeypatch.setattr(serve, "CAP_CHARS", 300)
        first = session.call(name, args)
        head = first["content"][0]["text"]
        assert "[truncated" in head and first["structuredContent"]["truncated"]
        pieces = [head.split("\n[truncated")[0] + "\n"]
        handle_id = first["structuredContent"]["handle"]
        while True:
            more = session.call("kg_more", {"handle": handle_id})
            assert not more.get("isError")
            pieces.append(more["content"][0]["text"].split("\n[more:")[0])
            if not more["structuredContent"]["remaining"]:
                break
        assert "".join(pieces) == full
        monkeypatch.undo()


def kin(name: str) -> str:
    return f"symbol:app:app/{name}"


def test_impact_follows_inherits_and_overrides_and_names_every_relation(tmp_path, kg_env):
    project = built_kin(tmp_path, kg_env)
    session = Session(project, "agent")
    result = session.call("kg_impact", {"id": kin("shapes.py::Base.run"), "depth": 1})
    rows = {r["node"]["id"]: (r["rel"], r["rels"], r["to"]) for r in result["structuredContent"]["impacted"]}
    base_run = kin("shapes.py::Base.run")
    assert rows == {
        kin("kids.py::Child.run"): ("calls", ["calls", "overrides"], base_run),  # super().run() in an override
        kin("kids.py::Plain.go"): ("calls", ["calls"], base_run),  # self.run() in a subclass that keeps run
        kin("kids.py::Quiet.run"): ("overrides", ["overrides"], base_run),
    }
    text = result["content"][0]["text"]
    assert "-calls-> [app:app/shapes.py::Base.run] (resolved) (also overrides)" in text
    assert "-overrides-> [app:app/shapes.py::Base.run] (resolved)\n" in text + "\n"
    subclasses = session.call("kg_impact", {"id": kin("shapes.py::Base"), "depth": 1})["structuredContent"]
    inherits = {r["node"]["id"] for r in subclasses["impacted"] if r["rel"] == "inherits"}
    assert inherits == {kin("kids.py::Child"), kin("kids.py::Plain"), kin("kids.py::Quiet")}


def test_impact_notes_the_coverage_of_its_seeds_languages(tmp_path, kg_env):
    project = built_kin(tmp_path, kg_env)
    session = Session(project, "agent")
    result = session.call("kg_impact", {"id": RANK, "paths": ["app:app/Shape.swift"]})
    coverage = {row["language"]: row for row in result["structuredContent"]["coverage"]}
    assert sorted(coverage) == ["python", "swift"]
    assert coverage["python"] == {"language": "python", **coverage_of("python")}
    assert all(coverage["python"][key]["supported"] for key in TABLES)
    swift = coverage["swift"]
    assert not any(swift[key]["supported"] for key in TABLES)
    assert swift["calls_across_files"]["note"] == CROSS_FILE_CALLS["swift"].note
    text = result["content"][0]["text"]
    missing = CROSS_FILE_CALLS["python"].missing
    assert f"coverage of python seeds: calls across files yes, not: {missing}; inheritance yes; overrides yes" in text
    assert f"coverage of swift seeds: calls across files no ({CROSS_FILE_CALLS['swift'].note});" in text
    # A seed that is no code gets no note; the field is there all the same, for clients that read it.
    plain = session.call("kg_impact", {"id": "usecase:KB-01"})
    assert plain["structuredContent"]["coverage"] == [] and "coverage of" not in plain["content"][0]["text"]


def test_impact_reads_every_edge_into_a_seed_and_says_when_it_cuts(tmp_path, kg_env, monkeypatch):
    project = built(tmp_path, kg_env)
    add_edge(project, "harness:file:knowledge.yaml", "depends_on", RANK, "parsed", level=2)  # hidden from agent
    add_edge(project, SERVER_PY, "calls", RANK, "proposed")  # never followed, never counted
    agent, wide = Session(project, "agent"), Session(project, "wide")
    whole = agent.call("kg_impact", {"id": RANK})
    assert whole["structuredContent"]["cut"] == [] and "cut:" not in whole["content"][0]["text"]

    monkeypatch.setattr(serve, "SEED_EDGES", 1)  # a page of one edge: every edge is still read
    assert agent.call("kg_impact", {"id": RANK}) == whole

    # RANK has a calls, a defines and a mentions edge the agent sink sees, and a fourth only the wide one does.
    monkeypatch.setattr(serve, "IMPACT_SEED_EDGES", 1)
    cut = agent.call("kg_impact", {"id": RANK})
    data = cut["structuredContent"]
    assert data["cut"] == [{"seed": RANK, "read": 1, "total": 3}]
    # Only the first edge by rel, calls from search, was read: search.py comes back through search, not RANK.
    assert impacted(cut) == {
        SEARCH: (1, "calls", "resolved"),
        HANDLE: (2, "calls", "resolved"),
        SEARCH_PY: (2, "defines", "parsed"),
    }
    assert (
        "cut: 3 edges lead into [app:app/search.py::rank_results] and the first 1 were read"
        in cut["content"][0]["text"]
    )
    assert data["summary"].endswith("; 1 seed(s) cut")
    assert wide.call("kg_impact", {"id": RANK})["structuredContent"]["cut"][0]["total"] == 4
    # The old fields keep their shape: a client that reads only these sees what it saw before.
    entry = whole["structuredContent"]["impacted"][0]
    assert {"hop", "node", "rel", "status", "to", "hub"} <= set(entry)


def test_docs_and_skill_print_the_coverage_tables():
    root = Path(__file__).parents[2]
    names = {
        "Python": "python",
        "TypeScript": "typescript",
        "JavaScript": "javascript",
        "Vue": "vue",
        "Go": "go",
        "Java": "java",
        "Kotlin": "kotlin",
        "Rust": "rust",
        "Ruby": "ruby",
        "PHP": "php",
        "C#": "csharp",
        "Swift": "swift",
        "Dart": "dart",
        "Shell": "shell",
        "SQL": "sql",
    }
    expected = {
        lang: tuple("yes" if TABLES[key][lang].supported else "no" for key in TABLES) for lang in names.values()
    }
    for path in (root / "docs" / "kg.md", root / "plugins/evo-kg/skills/using-project-graph/SKILL.md"):
        rows = re.findall(
            r"^\| (\S+) \| (yes|no)\b[^|]*\| (yes|no)\b[^|]*\| (yes|no)\b[^|]*\|$", path.read_text("utf-8"), re.M
        )
        assert {names[name]: cells for name, *cells in rows} == {k: list(v) for k, v in expected.items()}, path


# The text of the tools: readable ids, labels once, budgets and caps, the argument names agents guess


def test_readable_ids_name_files_sections_and_symbols_and_map_back():
    assert readable(SEARCH) == "app:app/search.py::search"
    assert readable(SEARCH_PY) == "app:app/search.py"
    assert readable("app:file:docs/guide.md#setup") == "app:docs/guide.md#setup"
    for other in ("plan:demo/step:2", "repo:app", "seam:search-api", "usecase:KB-01", "dir:app:docs", "source:app"):
        assert readable(other) == other
    # A source named like another kind's prefix keeps its stored ids, so a readable id never reads as a plan's.
    assert readable("plan:file:notes.md") == "plan:file:notes.md"
    assert readable("symbol:repo:a.py::f") == "symbol:repo:a.py::f"
    for node_id in (SEARCH, SEARCH_PY, "app:file:docs/guide.md#setup", "plan:demo/step:2"):
        assert node_id in stored(readable(node_id))
        assert stored(node_id)[0] == node_id


def test_every_tool_takes_readable_and_stored_ids_alike(tmp_path, kg_env):
    project = built_wider(tmp_path, kg_env)
    session = Session(project, "agent")
    pairs = [
        ("kg_node", {"id": SEARCH}, {"id": "app:app/search.py::search"}),
        ("kg_node", {"id": "app:file:docs/guide.md#setup"}, {"id": "app:docs/guide.md#setup"}),
        ("kg_context", {"ids": [SEARCH_PY]}, {"ids": ["app:app/search.py"]}),
        ("kg_impact", {"id": RANK}, {"id": "app:app/search.py::rank_results"}),
        (
            "kg_path",
            {"from": RANK, "to": SERVER_PY},
            {"from": "app:app/search.py::rank_results", "to": "app:app/server.py"},
        ),
    ]
    for name, by_stored, by_readable in pairs:
        stored_result = session.call(name, by_stored)
        assert not stored_result.get("isError"), (name, stored_result)
        assert session.call(name, by_readable)["structuredContent"] == stored_result["structuredContent"], name
    # A readable id copied from a result into kg_search finds that node first.
    found = session.call("kg_search", {"query": "app:app/search.py::search"})["structuredContent"]["results"]
    assert found[0]["id"] == SEARCH


def test_text_lines_carry_no_uri_and_print_a_shared_label_once(tmp_path, kg_env):
    project = built_wider(tmp_path, kg_env)
    session = Session(project, "agent")
    for name, args in QUERIES:
        result = session.call(name, args)
        text = result["content"][0]["text"]
        if name == "kg_node":
            continue  # kg_node gives the URI, below
        assert "uri=" not in text and "https://" not in text, name
        # Every node of the demo is internal: the label is printed once, under the first line, and on no node line.
        assert text.splitlines()[1] == "label of every node: internal,U", (name, text[:300])
        assert ", internal,U)" not in text, name
    card = session.call("kg_node", {"id": SEARCH_PY})["content"][0]["text"]
    assert card.startswith("[app:app/search.py] File (parsed, internal,U)")  # one node: its line names its label
    assert "github.com/acme/demo-app" in card  # kg_node still gives the URI, in its props and evidence

    # The app is customer-level: through the wide sink a result mixes labels; the common one is printed once and a
    # line names its node's own label only when it is another.
    wide = Session(built_wider(tmp_path / "mixed", kg_env, app_level="customer"), "wide")
    text = wide.call("kg_context", {"ids": ["usecase:KB-01"], "hops": 2})["content"][0]["text"]
    head, *lines = text.splitlines()
    common = lines[0].rsplit(": ", 1)[1]
    assert lines[0] == f"label of every node unless its line names another: {common}"
    other = {"internal,U": "customer,U", "customer,U": "internal,U"}[common]
    node_lines = [line for line in lines[1:] if line.startswith("[")]
    assert any(f", {other})" in line for line in node_lines) and not any(f", {common})" in line for line in node_lines)


def test_a_chat_message_line_names_its_chat_and_thread_and_never_its_text(tmp_path, kg_env):
    session = Session(built(tmp_path, kg_env), "agent")
    said = {
        "id": "plugin.chats:message:41",
        "kind": "ChatMessage",
        "name": "#7 alice 2026-10-19T08:00:00Z",
        "status": "parsed",
        "label": (1, 0, "U", frozenset({"chat:5"})),
        "props": {"chat": 5, "thread": 40, "seq": 7, "author_kind": "person", "author": "alice", "run": None},
    }
    # its id, its author and time (its name), its chat and its thread; its text is in no prop, and the others stay out
    assert session._line(said) == (
        "[plugin.chats:message:41] ChatMessage '#7 alice 2026-10-19T08:00:00Z' (parsed, internal,U audience chat:5) "
        "chat=5 thread=40"
    )
    # a node of any other kind that has such props prints them not: an item without an audience reads as before
    other = {**said, "id": "docs:doc:minutes", "kind": "Document", "name": "minutes", "label": (1, 0, "U")}
    assert session._line(other) == "[docs:doc:minutes] Document (parsed, internal,U)"  # the id ends with its name


def test_search_follows_a_step_or_a_section_to_the_code_it_mentions(tmp_path, kg_env):
    session = Session(built(tmp_path, kg_env), "agent")
    result = session.call("kg_search", {"query": "Wire the server to search", "limit": 5})
    lines = result["content"][0]["text"].splitlines()
    assert lines[2].startswith("[plan:demo/step:2] PlanStep 'Server'")  # its words: the step first
    assert lines[0].endswith(", status parsed")  # the status every result shares, printed once
    assert lines[3] == "  mentions [app:app/server.py] File"  # then the file its text names
    data = result["structuredContent"]
    assert [r["id"] for r in data["results"]][:2] == ["plan:demo/step:2", SERVER_PY]
    assert data["mentioned_by"] == {SERVER_PY: "plan:demo/step:2"}
    only_steps = session.call("kg_search", {"query": "Wire the server to search", "kinds": ["PlanStep"]})
    assert {r["kind"] for r in only_steps["structuredContent"]["results"]} == {"PlanStep"}
    assert only_steps["structuredContent"]["mentioned_by"] == {}

    # The app is customer-level and the agent sink clears internal: the step shows, the file it mentions does not.
    narrow = Session(built(tmp_path / "narrow", kg_env, app_level="customer"), "agent")
    hidden = narrow.call("kg_search", {"query": "Wire the server to search"})["structuredContent"]
    assert [r["id"] for r in hidden["results"]][:1] == ["plan:demo/step:2"]
    assert hidden["mentioned_by"] == {} and all(not r["id"].startswith("app:") for r in hidden["results"])


def test_search_lines_say_once_what_every_result_shares_and_few_results_when_none_holds_the_query(tmp_path, kg_env):
    session = Session(built(tmp_path, kg_env), "agent")
    text = session.call("kg_search", {"query": "Wire the server to search"})["content"][0]["text"].splitlines()
    assert text[0] == "10 result(s) in project demo (build 1), status parsed"  # the status every result shares
    assert text[2] == "[plan:demo/step:2] PlanStep 'Server' status=pending"  # its number is in its id
    assert "[app:app/server.py::Server] Symbol lines 4-6 words=1/3" in text  # no status, no label: both shared
    shown = [line.removeprefix("  mentions ") for line in text[2:] if line.lstrip().startswith(("[", "mentions ["))]
    assert len(shown) == 10 and all(" " in line for line in shown[: serve.FULL_LINES])
    assert all(re.fullmatch(r"\[[^\]]+\]", line) for line in shown[serve.FULL_LINES :])  # past the 5th: the id alone
    weak = session.call("kg_search", {"query": "server search zebra", "limit": 10})
    lines = weak["content"][0]["text"].splitlines()
    assert lines[2] == "no result holds all 3 words of the query"
    assert len(weak["structuredContent"]["results"]) == serve.WEAK_LIMIT  # none holds the query: the few best

    step = {
        "id": "plan:demo/step:3",
        "kind": "PlanStep",
        "name": "Wire",
        "status": "resolved",
        "label": (1, 0, "U"),
        "props": {"status": "done", "number": 3},
    }
    assert session._line(step, "public,U", status="parsed") == (
        "[plan:demo/step:3] PlanStep 'Wire' (resolved, internal,U) status=done"
    )
    assert session._line(step, "internal,U", status="resolved") == "[plan:demo/step:3] PlanStep 'Wire' status=done"
    assert session._line(step, "internal,U") == "[plan:demo/step:3] PlanStep 'Wire' (resolved) status=done number=3"
    section = {
        **step,
        "id": "app:file:docs/a.md#set-up",
        "kind": "Section",
        "name": "Set up",
        "props": {"anchor": "set-up"},
    }
    assert session._line(section, "internal,U", status="resolved") == "[app:docs/a.md#set-up] Section"


def test_search_says_how_many_words_each_result_holds_and_quotes_a_document(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    write(tmp_path / "demo-app" / "docs/quarry.md", "The zephyr quarry closed after the flood of March.\n")
    commit(tmp_path / "demo-app", "a note without a heading")
    sync_project(project)
    assert build_project(project).ok
    session = Session(project, "agent")
    result = session.call("kg_search", {"query": "zephyr quarry tuesday", "limit": 3})
    lines = result["content"][0]["text"].splitlines()
    assert "no result holds all 3 words of the query" in lines
    first = lines.index(next(line for line in lines if line.startswith("[app:docs/quarry.md] Document")))
    assert lines[first].endswith(" words=2/3")
    assert lines[first + 1] == "    > The zephyr quarry closed after the flood of March."
    assert result["structuredContent"]["matched"]["app:file:docs/quarry.md"]["words"] == 2
    # every word held: no note; one word: no count, the document still quoted
    both = session.call("kg_search", {"query": "zephyr quarry"})["content"][0]["text"]
    assert "no result holds" not in both and " words=" not in both  # a count only for a result short of words
    one = session.call("kg_search", {"query": "zephyr"})["content"][0]["text"].splitlines()
    assert not any("words=" in line for line in one) and "    > The zephyr quarry closed" in "\n".join(one)


def test_context_keeps_nodes_and_edges_within_its_budget_and_counts_the_rest(tmp_path, kg_env):
    project = built_wider(tmp_path, kg_env)
    session = Session(project, "agent")
    roomy = session.call("kg_context", {"query": "search", "budget_tokens": 8000, "hops": 3})["structuredContent"]
    assert roomy["left_out"] == {"nodes": 0, "edges": 0, "beyond": False}
    for budget in (500, 700, 1000, 3000):
        result = session.call("kg_context", {"query": "search", "budget_tokens": budget, "hops": 3})
        text, data = result["content"][0]["text"], result["structuredContent"]
        assert len(text.encode("utf-8")) / 3 <= budget, (budget, len(text.encode("utf-8")))
        shown = {e["src"] for e in data["edges"]} | {e["dst"] for e in data["edges"]}
        assert shown <= {n["id"] for n in data["nodes"]}  # no edge to a node the text does not show
        assert text.count("\n  [") == len(data["edges"])  # every edge it keeps is in the text
    tight = session.call("kg_context", {"query": "search", "budget_tokens": 500, "hops": 3})
    left = tight["structuredContent"]["left_out"]
    assert left["nodes"] + left["edges"] > 0 or left["beyond"]
    assert len(tight["structuredContent"]["nodes"]) < len(roomy["nodes"])
    assert "left out by the budget:" in tight["content"][0]["text"]


def test_impact_lists_the_first_dependents_of_each_hop_and_counts_the_rest(tmp_path, kg_env):
    project = built_wider(tmp_path, kg_env)
    session = Session(project, "agent")
    whole = session.call("kg_impact", {"id": RANK})["structuredContent"]
    assert whole["limit"] == serve.IMPACT_LIMIT and whole["left_out"]["hops"] == {}
    # Documents are listed a quarter as many: 5 of the 6 that mention what RANK reaches.
    assert (len(whole["documents"]), whole["left_out"]["documents"]) == (6, 1)
    capped = session.call("kg_impact", {"id": RANK, "limit": 1})
    text, data = capped["content"][0]["text"], capped["structuredContent"]
    assert impacted(capped) == impacted({"structuredContent": whole})  # the data keeps every dependent
    per_hop = {hop: sum(r["hop"] == hop for r in data["impacted"]) for hop in (1, 2)}
    assert per_hop == {1: 2, 2: 4}
    assert data["left_out"] == {"hops": {"1": 1, "2": 3}, "documents": len(data["documents"]) - 1}
    assert "hop 1: first 1 of 2\n" in text and "  ... 1 more at hop 1: raise limit" in text
    assert "hop 2: first 1 of 4\n" in text and "  ... 3 more at hop 2: raise limit" in text
    # The strongest edge first: at hop 1 the file defines rank_results (parsed), search calls it (resolved).
    assert "hop 1: first 1 of 2\n  [app:app/search.py] File (parsed) -defines->" in text
    assert text.count(" -mentions-> ") == 1 and f"documents to re-check: first 1 of {len(data['documents'])}" in text


def test_impact_of_store_py_on_a_real_sized_graph_stays_short(tmp_path, kg_env):
    # Thirty files that import one module, each named in a plan step: two hops and a document group over the limit.
    project = make_project(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    for i in range(30):
        write(
            app / f"app/user_{i:02d}.py", f"from app.search import search\n\n\ndef use_{i}(q):\n    return search(q)\n"
        )
    commit(app, "users")
    sync_project(project)
    assert build_project(project).ok
    result = Session(project, "agent").call("kg_impact", {"paths": ["app:app/search.py"]})
    text, data = result["content"][0]["text"], result["structuredContent"]
    assert len(data["impacted"]) > serve.IMPACT_LIMIT
    assert sum(line.startswith("  [") for line in text.splitlines()) <= 2 * serve.IMPACT_LIMIT + 1 + 5
    assert f"more at hop 1: raise limit (up to {serve.IMPACT_MAX_LIMIT})" in text


def test_tools_take_the_argument_names_agents_guess(tmp_path, kg_env):
    project = built_wider(tmp_path, kg_env)
    session = Session(project, "agent")

    def same(name: str, guessed: dict, own: dict) -> None:
        got, want = session.call(name, guessed), session.call(name, own)
        assert not want.get("isError") and got == want, (name, guessed, got)

    # The names Codex sent kg_impact and kg_context between 2026-09-20 and 2026-10-17.
    for key in ("files", "targets", "changed"):
        same("kg_impact", {key: ["app:app/search.py"]}, {"paths": ["app:app/search.py"]})
    for key in ("target", "name"):
        same("kg_impact", {key: "app/search.py"}, {"paths": ["app/search.py"]})
    for key in ("budget", "max_tokens"):
        same("kg_context", {"query": "search", key: 600}, {"query": "search", "budget_tokens": 600})
    same("kg_context", {"id": "usecase:KB-01"}, {"ids": ["usecase:KB-01"]})
    same("kg_path", {"source": RANK, "target": SERVER_PY}, {"from": RANK, "to": SERVER_PY})
    same("kg_node", {"node_id": SEARCH}, {"id": SEARCH})
    same("kg_search", {"q": "search", "kind": "Symbol"}, {"query": "search", "kinds": ["Symbol"]})
    # A path given as an id, and an id given as a path.
    same("kg_impact", {"id": "app/search.py"}, {"paths": ["app/search.py"]})
    same("kg_impact", {"paths": [RANK]}, {"id": RANK})

    wrong = session.call("kg_impact", {"id": RANK, "budget_tokens": 2000})
    assert wrong["isError"]
    assert wrong["content"][0]["text"] == (
        "error: kg_impact has no argument 'budget_tokens'; it takes deprecated, depth, diff, id, ids, limit, paths"
    )
    assert "Session." not in session.call("kg_search", {"query": "x", "unknown": 1})["content"][0]["text"]
    assert session.call("kg_impact", {"id": RANK, "limit": "many"})["isError"]  # a bad value is an error, not a crash


def test_instructions_descriptions_and_skill_name_only_statuses_the_graph_has():
    root = Path(__file__).parents[2]
    skill = (root / "plugins/evo-kg/skills/using-project-graph/SKILL.md").read_text("utf-8")
    texts = [serve.INSTRUCTIONS, skill, json.dumps(serve.TOOLS)]
    for text in texts:
        for status in ("declared", "proposed", "introspected"):
            assert status not in text, status
    for text in (serve.INSTRUCTIONS, skill):
        assert "'parsed'" in text or "`parsed`" in text
        assert "'resolved'" in text or "`resolved`" in text


def test_results_print_the_lines_of_a_section_a_symbol_and_the_passage_a_long_section_matched(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    filler = "Filler sentence about nothing. " * 200
    write(tmp_path / "demo-app" / "docs/long.md", f"# Long\n\n{filler}\n\nzanzibarquokka at the end.\n")
    commit(tmp_path / "demo-app", "a long section")
    sync_project(project)
    assert build_project(project).ok
    session = Session(project, "agent")
    found = session.call("kg_search", {"query": "zanzibarquokka", "limit": 3})
    lines = found["content"][0]["text"].splitlines()
    hit = next(line for line in lines if line.startswith("[app:docs/long.md#long] Section"))
    assert hit.endswith(" lines 4-5 of 1-5"), hit  # the passage that holds the word, within the section
    assert found["structuredContent"]["matched"]["app:file:docs/long.md#long"]["lines"] == "lines 4-5"

    guide = session.call("kg_search", {"query": "rank_results internally"})["content"][0]["text"]
    assert "[app:docs/guide.md#details] Section lines 9-12" in guide  # a short one: its own lines; its anchor names it
    symbol = session.call("kg_search", {"query": "rank_results"})["content"][0]["text"]
    assert "[app:app/search.py::rank_results] Symbol lines 1-2" in symbol  # first and last line
    node = session.call("kg_node", {"id": "app:file:docs/guide.md#details"})["content"][0]["text"]
    assert "lines 9-12" in node.splitlines()[0]
    context = session.call("kg_context", {"query": "rank_results", "budget_tokens": 4000})["content"][0]["text"]
    assert "[app:app/search.py::rank_results] Symbol (parsed) lines 1-2" in context


class Always(random.Random):
    """A draw that always explores, and picks the first candidate and the last rank."""

    def random(self):
        return 0.0

    def choice(self, seq):
        return seq[-1]

    def randrange(self, n):
        return 0


def test_a_search_explores_only_with_an_explorer_and_says_where_and_with_what_chance(tmp_path, kg_env):
    project = built(tmp_path, kg_env)
    plain = Session(project, "agent")
    query = {"query": "search", "limit": 5}
    before = plain.call("kg_search", query)
    assert all("explored:" not in plain.call("kg_search", query)["content"][0]["text"] for _ in range(20))
    shown = [r["id"] for r in before["structuredContent"]["results"]]
    everything = [r["id"] for r in plain.call("kg_search", {**query, "limit": 25})["structuredContent"]["results"]]
    past = [i for i in everything if i not in shown]
    assert len(shown) == 5 and past

    exploring = Session(project, "agent")
    exploring.explorer = Explorer(rate=0.05, rng=Always())
    result = exploring.call("kg_search", query)
    text = result["content"][0]["text"]
    ids = [r["id"] for r in result["structuredContent"]["results"]]
    assert len(ids) == 5 and ids[:4] == shown[:4] and ids[4] in past  # rank 5: a hit from past the results
    rank, chance, ref = explore.marked(text)
    assert (rank, ref) == (5, readable(ids[4]))
    assert 0 < chance <= 0.05 / 2
    assert text.splitlines()[-1].startswith("explored: rank 5, p=")

    never = Session(project, "agent")
    never.explorer = Explorer(rate=0.0, rng=Always())
    assert [r["id"] for r in never.call("kg_search", query)["structuredContent"]["results"]] == shown


def test_an_explorer_draws_at_its_rate_and_needs_results_to_put_a_candidate_after():
    pick = Explorer(rate=1.0, rng=random.Random(3)).pick
    assert pick([1, 2], ["c"]) is None  # fewer than three results: no rank 4 to go to
    index, rank, chance = pick([1, 2, 3], ["c", "d"])
    assert rank == 4 and index in (0, 1) and chance == pytest.approx(1.0 / 1 / 2)
    assert pick([1, 2, 3, 4, 5], []) is None
    assert Explorer(rng=Always()).pick([1, 2, 3, 4, 5], ["c"]) is None  # tests run at RATE 0 (tests/conftest.py)
    drawn = Explorer(rate=0.05, rng=random.Random(1))  # the hub's RATE
    explored = sum(drawn.pick([1, 2, 3, 4, 5], ["c"]) is not None for _ in range(4000))
    assert 120 < explored < 280  # about one search in twenty


def built_with_code_text(tmp_path, home):
    """The demo project plus a setting read in a view, a template and a stylesheet naming a selector and a class."""
    project = make_project(tmp_path, home)
    app = tmp_path / "demo-app"
    write(app / "app/settings.py", "LUMEN_CARDS_ENABLED = True\n")
    write(
        app / "app/views.py",
        "from app import settings\n\n\ndef page():\n    return settings.LUMEN_CARDS_ENABLED\n",
    )
    write(app / "app/templates/card.html", '<div class="course-card">\n  <app-course-card></app-course-card>\n</div>\n')
    write(app / "app/styles/card.scss", ".course-card {\n  color: red;\n}\n")
    commit(app, "cards")
    sync_project(project)
    report = build_project(project)
    assert report.ok, report.errors
    return project


def test_search_finds_where_a_name_is_written_in_code_templates_and_stylesheets(tmp_path, kg_env):
    session = Session(built_with_code_text(tmp_path, kg_env), "agent")
    flag = session.call("kg_search", {"query": "LUMEN_CARDS_ENABLED"})
    data = flag["structuredContent"]
    ids = [r["id"] for r in data["results"]]
    assert "app:file:app/views.py" in ids  # where it is read: no edge of the graph says so
    assert data["code"]["app:file:app/views.py"] == [5]
    assert "[app:app/views.py] File line 5 code" in flag["content"][0]["text"].splitlines()

    selector = session.call("kg_search", {"query": "app-course-card"})["structuredContent"]
    assert selector["code"].get("app:file:app/templates/card.html") == [2]  # one word: the dashes are inside it
    css = session.call("kg_search", {"query": "course-card"})["structuredContent"]
    assert css["code"].get("app:file:app/styles/card.scss") == [1]
    assert css["code"].get("app:file:app/templates/card.html") == [1]  # app-course-card is another word

    # A question in prose looks up only the words written like identifiers.
    prose = session.call("kg_search", {"query": "Wire the server to search"})["structuredContent"]
    assert prose["code"] == {}
    asked = session.call("kg_search", {"query": "where do the views read LUMEN_CARDS_ENABLED"})["structuredContent"]
    assert "app:file:app/views.py" in asked["code"]

    # Template text is not in the index of prose: a document still answers a question in prose first.
    store = Store.for_project(session.project)
    assert store.db.execute("SELECT count(*) FROM node_fts WHERE body LIKE '%app-course-card%'").fetchone()[0] == 0
    # A word more files hold than are read per word names a kind of file, not one: it is left out.
    assert {nid for nid, _ in store.code_files(["course-card"], limit=2)} == {
        "app:file:app/templates/card.html",
        "app:file:app/styles/card.scss",
    }
    assert store.code_files(["course-card"], limit=1) == []
    store.close()


def test_search_keeps_to_the_repo_it_is_given(tmp_path, kg_env):
    session = Session(built(tmp_path, kg_env), "agent")
    wide = session.call("kg_search", {"query": "search"})["structuredContent"]["results"]
    assert any(readable(r["id"]).partition(":")[0] != "app" for r in wide)  # steps, seams and the harness too
    for repo in ("app", "APP"):
        only = session.call("kg_search", {"query": "search", "repo": repo})["structuredContent"]["results"]
        assert only and all(serve.in_repo(r, "app") for r in only)
    none = session.call("kg_search", {"query": "search", "repo": "northwind"})
    assert none["structuredContent"]["results"] == []
    assert "nothing of repo 'northwind' matched" in none["content"][0]["text"]


def test_code_words_read_a_short_query_whole_and_a_long_one_for_identifiers():
    from evo_agents.kg.search import code_words, lines_holding

    assert code_words("imsmanifest .hostname") == ["imsmanifest", "hostname"]
    assert code_words("sync_activity_logs SYNC_INTERVAL") == ["sync_activity_logs", "SYNC_INTERVAL"]
    assert code_words("where is the setting read in every view of the app") == []
    assert code_words("where is getCourseCard called in the lumen app") == ["getCourseCard"]
    assert code_words("ab 12 the") == []
    text = "x = 1\nLUMEN_FLAG = 2\nprint(LUMEN_FLAG_OLD)\n  lumen_flag()\n"
    assert lines_holding(text, ["LUMEN_FLAG"]) == [2, 4]  # whole words only, case aside
