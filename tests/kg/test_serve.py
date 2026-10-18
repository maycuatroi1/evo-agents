import json
import re
from pathlib import Path

from evo_agents.kg import serve
from evo_agents.kg.build import build_project
from evo_agents.kg.extract.languages import CROSS_FILE_CALLS, TABLES, coverage_of
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
    assert f"[{SEARCH}] Symbol 'search'" in text and f"-calls-> [{RANK}] (resolved)" in text
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
    assert f"<-defines- (parsed) [{SEARCH_PY}]" in result["content"][0]["text"]
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
    assert f"-calls-> [{base_run}] (resolved) (also overrides)" in text
    assert f"-overrides-> [{base_run}] (resolved)\n" in text + "\n"
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
    assert f"cut: 3 edges lead into [{RANK}] and the first 1 were read" in cut["content"][0]["text"]
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
