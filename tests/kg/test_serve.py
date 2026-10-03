import json
from pathlib import Path

from evo_agents.kg import serve
from evo_agents.kg.build import build_project
from evo_agents.kg.serve import Session, diff_lines, handle
from evo_agents.kg.store import Store, edge_id
from evo_agents.kg.sync import sync_project
from tests.kg.projects import commit, make_project, write

RANK = "symbol:app:app/search.py::rank_results"
SEARCH = "symbol:app:app/search.py::search"
TEST_SEARCH = "symbol:app:tests/test_search.py::test_search"
SEARCH_PY = "app:file:app/search.py"
SERVER_PY = "app:file:app/server.py"

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
    add_edge(project, "symbol:app:app/server.py::Server.handle", "calls", SEARCH, "proposed")
    add_edge(project, "symbol:app:app/server.py::Server", "calls", SEARCH, "introspected")
    add_edge(project, "harness:file:CLUSTER.md#demo", "mentions", SEARCH, "proposed")
    result = Session(project, "agent").call("kg_impact", {"id": SEARCH, "depth": 1})
    got = impacted(result)
    assert got["symbol:app:app/server.py::Server"] == (1, "calls", "introspected")
    assert "symbol:app:app/server.py::Server.handle" not in got
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
