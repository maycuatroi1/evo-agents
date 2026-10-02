import json

from evo_agents.kg import serve
from evo_agents.kg.build import build_project
from evo_agents.kg.serve import Session, handle
from evo_agents.kg.store import Store
from evo_agents.kg.sync import sync_project
from tests.kg.projects import make_project

QUERIES = [
    ("kg_search", {"query": "search"}),
    ("kg_search", {"query": "KB-01"}),
    ("kg_node", {"id": "plan:demo"}),
    ("kg_node", {"id": "symbol:app:app/search.py::search"}),
    ("kg_context", {"query": "search", "budget_tokens": 2000}),
    ("kg_context", {"ids": ["usecase:KB-01"], "hops": 2}),
]


def built(tmp_path, home, **kw):
    project = make_project(tmp_path, home, **kw)
    sync_project(project)
    report = build_project(project)
    assert report.ok, report.errors
    return project


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


def test_unbound_server_explains_how_to_bind(tmp_path, kg_env, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("EVO_KG_PROJECT", raising=False)
    session = serve.make_session(None, "agent")
    result = session.call("kg_search", {"query": "x"})
    assert result["isError"] and "--project" in result["content"][0]["text"]
