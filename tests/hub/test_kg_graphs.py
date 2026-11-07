"""Named graphs of a project (``docs/kg.md``, Named graphs): ``evo_agents.kg.graph_scope``, ``evo_agents.hub.
kg_named_graphs``, the routes of ``evo_agents.hub.server.kg_graphs`` and ``evo-agents hub kg graph``.

Checked here, on the fixture of ``tests.hub.kg_fixture`` (two sources: docs and deals) built on this machine: a kg
tool call without ``graph`` answers exactly as one with ``graph="default"``; with a named graph, kg_search,
kg_context, kg_node, kg_path and kg_impact return only the nodes and edges of its sources, and the labels the sink
clears still filter; an unknown name is an error naming ``evo-agents hub kg graph list``; the tool schemas gain
``graph`` and nothing else; the cross-project read and ``worker agent read --graph`` pass it on; the CLI calls the
routes. With a test database (``EVO_HUB_TEST_DSN``): the routes create, list, update and delete named graphs for a
writer and refuse a reader, a name taken and ``default``; the hub's tool route and the web search narrow by them.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from evo_agents.hub import kg_graphs_cli, kg_named_graphs
from evo_agents.hub.mcp_tools import KG_TOOLS
from evo_agents.kg.serve import Session
from tests.hub import live, pg
from tests.hub.kg_fixture import (
    AGENT,
    CONTRACT_SECTION,
    NARROW,
    RUNBOOK,
    RUNBOOK_SECTION,
    SHARED_NODE,
    Harness,
    registration,
)

NAMED = {"deals-only": frozenset({"deals"}), "docs-only": frozenset({"docs"})}
NARROWED = ("kg_search", "kg_context", "kg_node", "kg_path", "kg_impact")


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    from evo_agents.kg.build import build_project

    harness = Harness(tmp_path_factory.mktemp("kg-graphs"), "kggraphs")
    harness.sync()
    build_project(harness.project())
    return harness.project()


def session(project, sink: str = AGENT) -> Session:
    found = Session(project, sink=sink)
    found.named_graphs = dict(NAMED)
    return found


def call(project, tool: str, arguments: dict, sink: str = AGENT) -> dict:
    return session(project, sink).call(tool, arguments)


def ids(result: dict) -> set[str]:
    text = json.dumps(result.get("structuredContent", {}), ensure_ascii=False)
    return set(re.findall(r'"id": "([^"]+)"', text))


def text(result: dict) -> str:
    return json.dumps(result.get("structuredContent", {}), ensure_ascii=False)


# The kg tools


@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("kg_search", {"query": "KB-01"}),
        ("kg_node", {"id": SHARED_NODE}),
        ("kg_context", {"ids": [SHARED_NODE]}),
        ("kg_impact", {"id": SHARED_NODE}),
        ("kg_path", {"from": RUNBOOK_SECTION, "to": CONTRACT_SECTION}),
    ],
)
def test_a_call_without_graph_answers_as_default_does(project, tool, arguments):
    without = call(project, tool, arguments)
    assert not without.get("isError"), without
    assert without == call(project, tool, {**arguments, "graph": "default"})


def test_search_with_a_named_graph_finds_only_its_sources(project):
    every = ids(call(project, "kg_search", {"query": "KB-01"}))
    deals = ids(call(project, "kg_search", {"query": "KB-01", "graph": "deals-only"}))
    docs = ids(call(project, "kg_search", {"query": "KB-01", "graph": "docs-only"}))
    assert any(i.startswith("docs:") for i in every) and any(i.startswith("deals:") for i in every)
    assert deals and not any(i.startswith("docs:") for i in deals)
    assert docs and not any(i.startswith("deals:") for i in docs)


@pytest.mark.parametrize(
    "tool, arguments", [("kg_context", {"ids": [SHARED_NODE]}), ("kg_impact", {"id": SHARED_NODE})]
)
def test_context_and_impact_with_a_named_graph_stay_in_its_sources(project, tool, arguments):
    every = text(call(project, tool, arguments))
    deals = call(project, tool, {**arguments, "graph": "deals-only"})
    assert not deals.get("isError"), deals
    assert "docs:" in every
    assert "docs:" not in text(deals)


def test_a_node_of_a_named_graph_shows_only_the_edges_and_evidence_of_its_sources(project):
    every = text(call(project, "kg_node", {"id": SHARED_NODE}))
    deals = call(project, "kg_node", {"id": SHARED_NODE, "graph": "deals-only"})
    assert not deals.get("isError"), deals
    assert "docs:" in every and "deals:" in every
    assert "docs:" not in text(deals) and "deals:" in text(deals)


def test_a_node_outside_the_named_graph_is_not_there(project):
    assert not call(project, "kg_node", {"id": RUNBOOK}).get("isError")
    found = call(project, "kg_node", {"id": RUNBOOK, "graph": "deals-only"})
    assert found.get("isError") and "no visible node" in found["content"][0]["text"]


def test_a_path_across_two_sources_is_not_in_a_graph_of_one(project):
    assert not call(project, "kg_path", {"from": RUNBOOK_SECTION, "to": CONTRACT_SECTION}).get("isError")
    for name in NAMED:
        found = call(project, "kg_path", {"from": RUNBOOK_SECTION, "to": CONTRACT_SECTION, "graph": name})
        assert found.get("isError"), (name, found)


def test_labels_still_filter_inside_a_named_graph(project):
    agent = ids(call(project, "kg_search", {"query": "Acme", "graph": "deals-only"}))
    narrow = ids(call(project, "kg_search", {"query": "Acme", "graph": "deals-only"}, sink=NARROW))
    assert any(i.startswith("deals:") for i in agent)
    assert narrow <= agent and not any(i.startswith("deals:") for i in narrow)


def test_an_unknown_graph_is_an_error_naming_the_list_command(project):
    found = call(project, "kg_search", {"query": "KB-01", "graph": "nope"})
    assert found.get("isError")
    assert "evo-agents hub kg graph list" in found["content"][0]["text"]


def test_the_tool_schemas_gain_graph_on_the_five_tools_only():
    by_name = {tool["name"]: tool for tool in KG_TOOLS}
    for name in NARROWED:
        assert "graph" in by_name[name]["inputSchema"]["properties"], name
        assert "graph" not in by_name[name]["inputSchema"].get("required", []), name
    for name in set(by_name) - set(NARROWED):
        assert "graph" not in by_name[name]["inputSchema"].get("properties", {}), name


# Names and sources


@pytest.mark.parametrize("name", ["deals", "lumen-ops", "a.b_c-1", "0" * 63])
def test_a_graph_name(name):
    assert kg_named_graphs.check_name(name) == name


@pytest.mark.parametrize("name", ["default", "", "Deals", "-x", "a b", "a/b", "x" * 64])
def test_not_a_graph_name(name):
    with pytest.raises(kg_named_graphs.GraphError):
        kg_named_graphs.check_name(name)


def test_sources_lose_repeats_and_refuse_what_cannot_be_a_source():
    assert kg_named_graphs.check_sources(["deals", "docs", "deals"]) == ["deals", "docs"]
    for bad in (["a/b"], [""], ["x\ny"], ["x" * 201]):
        with pytest.raises(kg_named_graphs.GraphError):
            kg_named_graphs.check_sources(bad)
    with pytest.raises(kg_named_graphs.GraphError):
        kg_named_graphs.check_sources([f"s{i}" for i in range(kg_named_graphs.MAX_SOURCES + 1)])


# The cross-project read


def test_the_cross_project_read_passes_graph_on_to_the_kg_tool():
    from evo_agents.hub.server.agent_reads import CrossReadIn, _arguments

    read = CrossReadIn(project="lumen-ops", tool="kg_search", query="KB-01", graph="deals-only")
    assert _arguments(read)["graph"] == "deals-only"
    assert "graph" not in _arguments(CrossReadIn(project="lumen-ops", tool="kg_search", query="KB-01"))
    context = CrossReadIn(project="lumen-ops", tool="kg_context", ids=[SHARED_NODE], graph="deals-only")
    assert _arguments(context) == {"ids": [SHARED_NODE], "graph": "deals-only"}


def test_agent_read_graph_goes_in_the_body_of_a_kg_read_only():
    from evo_agents.worker import agent_read_cli, chat_cli

    args = SimpleNamespace(project="lumen-ops", tool="kg-search", limit=5, text="KB-01", ids=None, graph="deals-only")
    assert agent_read_cli._body(args)["graph"] == "deals-only"
    memory = SimpleNamespace(project="lumen-ops", tool="memory-search", limit=5, text="x", ids=None, graph="deals")
    with pytest.raises(chat_cli.WorkerStateError):
        agent_read_cli._body(memory)


# The CLI


class FakeHub:
    def __init__(self):
        self.calls: list[tuple] = []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        graph = {"name": "deals-only", "description": "", "sources": ["deals"], "default": False, "missing": []}
        if method == "GET":
            default = {"name": "default", "description": "", "sources": [], "default": True, "missing": []}
            return {"graphs": [default, graph], "sources": ["deals", "docs"], "can_change": True}
        if method == "DELETE":
            return {"project": "northwind", "deleted": "deals-only"}
        return graph


@pytest.fixture
def fake_hub(monkeypatch):
    hub = FakeHub()
    monkeypatch.setattr(kg_graphs_cli, "signed_in", lambda: (hub, None))
    return hub


def run_cli(*argv) -> int:
    from evo_agents.cli import main

    return main(["hub", "kg", "graph", *argv])


def test_the_cli_calls_the_routes(fake_hub, capsys):
    base = "/v1/kg/northwind/graphs"
    assert run_cli("create", "deals-only", "--project", "northwind", "--source", "deals") == 0
    assert run_cli("update", "deals-only", "--project", "northwind", "--add", "docs", "--remove", "deals") == 0
    assert run_cli("update", "deals-only", "--project", "northwind", "--rename", "sales") == 0
    assert run_cli("delete", "deals-only", "--project", "northwind") == 0
    assert "deleted graph deals-only of project northwind" in capsys.readouterr().out
    assert run_cli("list", "--project", "northwind", "--json") == 0
    assert fake_hub.calls == [
        ("POST", base, {"name": "deals-only", "sources": ["deals"], "description": ""}),
        ("PATCH", f"{base}/deals-only", {"add": ["docs"], "remove": ["deals"]}),
        ("PATCH", f"{base}/deals-only", {"add": [], "remove": [], "rename": "sales"}),
        ("DELETE", f"{base}/deals-only", None),
        ("GET", base, None),
    ]
    listed = json.loads(capsys.readouterr().out)
    assert listed["project"] == "northwind" and [g["name"] for g in listed["graphs"]] == ["default", "deals-only"]


def test_an_update_that_changes_nothing_is_refused_before_the_hub(fake_hub, capsys):
    assert run_cli("update", "deals-only", "--project", "northwind") == 2
    assert fake_hub.calls == [] and "say what changes" in capsys.readouterr().err


# The hub (needs a test database)

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
PROJECT = "kggraphs"
MEMBERS = {"wendy": ("writer", "customer"), "ivan": ("reader", "customer")}


@pytest.fixture(scope="module")
def hub(pg_server, tmp_path_factory):
    from fastapi.testclient import TestClient

    from evo_agents.hub.server.app import create_app
    from tests.hub.s3 import fake_s3
    from tests.hub.test_kg import InProcessHub, make_config, run_jobs

    base = tmp_path_factory.mktemp("kg-graphs-api")
    db = pg.create_database()
    try:
        with fake_s3() as s3:
            app = create_app(make_config(db, base / "api", s3))
            with TestClient(app) as client:
                admin = live.bearer(live.insert_token(db, live.ADMIN))
                response = client.put(f"/v1/projects/{PROJECT}", json=registration(PROJECT), headers=admin)
                assert response.status_code == 200, response.text
                members = {}
                for login, (role, level) in MEMBERS.items():
                    granted = client.put(
                        f"/v1/admin/projects/{PROJECT}/grants/{login}",
                        json={"role": role, "max_level": level},
                        headers=admin,
                    )
                    assert granted.status_code in (200, 201), granted.text
                    members[login] = live.bearer(live.insert_token(db, login))
                harness = Harness(base / "machine", PROJECT)
                harness.sync()
                harness.push(InProcessHub(client, members["wendy"]))
                run_jobs(db, s3, base / "worker")
                yield SimpleNamespace(client=client, **members)
        assert pg.wait_no_backends(db.name) == 0
    finally:
        pg.drop_database(db)


def graphs_path(name: str = "") -> str:
    return f"/v1/kg/{PROJECT}/graphs" + (f"/{name}" if name else "")


def hub_tool(hub, name: str, arguments: dict) -> dict:
    response = hub.client.post(
        f"/v1/kg/{PROJECT}/tools/{name}", json={"arguments": arguments, "sink": AGENT}, headers=hub.wendy
    )
    assert response.status_code == 200, response.text
    return response.json()


@needs_pg
def test_a_writer_creates_lists_renames_and_deletes_a_named_graph(hub):
    c = hub.client
    made = c.post(graphs_path(), json={"name": "sales", "sources": ["deals", "gone"]}, headers=hub.wendy)
    assert made.status_code == 201, made.text
    assert made.json()["sources"] == ["deals", "gone"] and made.json()["missing"] == ["gone"]
    listed = c.get(graphs_path(), headers=hub.ivan).json()
    assert [g["name"] for g in listed["graphs"]] == ["default", "sales"] and listed["can_change"] is False
    assert set(listed["sources"]) >= {"deals", "docs"}
    changed = c.patch(graphs_path("sales"), json={"rename": "deals-only", "remove": ["gone"]}, headers=hub.wendy)
    assert changed.status_code == 200, changed.text
    assert (changed.json()["name"], changed.json()["sources"]) == ("deals-only", ["deals"])
    gone = c.delete(graphs_path("deals-only"), headers=hub.wendy)
    assert gone.status_code == 200 and gone.json() == {"project": PROJECT, "deleted": "deals-only"}
    assert [g["name"] for g in c.get(graphs_path(), headers=hub.wendy).json()["graphs"]] == ["default"]


@needs_pg
def test_the_routes_refuse_a_reader_a_name_taken_and_default(hub):
    c = hub.client
    assert c.post(graphs_path(), json={"name": "x"}, headers=hub.ivan).status_code == 403
    assert c.post(graphs_path(), json={"name": "dup"}, headers=hub.wendy).status_code == 201
    assert c.post(graphs_path(), json={"name": "dup"}, headers=hub.wendy).status_code == 409
    assert c.post(graphs_path(), json={"name": "default"}, headers=hub.wendy).status_code == 422
    assert c.patch(graphs_path("default"), json={"add": ["docs"]}, headers=hub.wendy).status_code == 422
    assert c.delete(graphs_path("default"), headers=hub.wendy).status_code == 422
    assert c.delete(graphs_path("nope"), headers=hub.wendy).status_code == 404
    assert c.delete(graphs_path("dup"), headers=hub.wendy).status_code == 200


@needs_pg
def test_the_hub_tools_and_the_web_search_narrow_by_a_named_graph(hub):
    c = hub.client
    assert c.post(graphs_path(), json={"name": "docs-only", "sources": ["docs"]}, headers=hub.wendy).status_code == 201
    try:
        every = ids(hub_tool(hub, "kg_search", {"query": "KB-01"}))
        docs = ids(hub_tool(hub, "kg_search", {"query": "KB-01", "graph": "docs-only"}))
        assert any(i.startswith("deals:") for i in every)
        assert docs and not any(i.startswith("deals:") for i in docs)
        assert hub_tool(hub, "kg_search", {"query": "KB-01", "graph": "default"}) == hub_tool(
            hub, "kg_search", {"query": "KB-01"}
        )
        unknown = hub_tool(hub, "kg_search", {"query": "KB-01", "graph": "nope"})
        assert unknown.get("isError") and "hub kg graph list" in unknown["content"][0]["text"]
        web = c.get(f"/v1/kg/{PROJECT}/nodes", params={"q": "KB-01", "graph": "docs-only"}, headers=hub.wendy)
        assert web.status_code == 200, web.text
        assert web.json()["named_graph"] == "docs-only"
        assert not any(n["id"].startswith("deals:") for n in web.json()["results"])
        missing = c.get(f"/v1/kg/{PROJECT}/nodes", params={"q": "KB-01", "graph": "nope"}, headers=hub.wendy)
        assert missing.status_code == 404
    finally:
        c.delete(graphs_path("docs-only"), headers=hub.wendy)
