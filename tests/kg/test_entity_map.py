"""The entity map an agent posts, read by the build of strategy entity-graph-agent with no model
(evo_agents.kg.entity_map; docs/kg-strategies.md, "entity-graph-agent")."""

from __future__ import annotations

import json

import pytest

from evo_agents.kg import entity_map, strategy
from evo_agents.kg.build import build_project, unchanged
from evo_agents.kg.project import load_project_at
from evo_agents.kg.schema import HUB_RELATIONS
from evo_agents.kg.store import Store
from evo_agents.kg.sync import sync_project
from tests.kg.projects import make_project

GUIDE = "app:file:docs/guide.md"
CLUSTER = "harness:file:CLUSTER.md"
SERVER = "symbol:app:app/server.py::Server"
SEARCH_FILE = "app:file:app/search.py"


def setup(tmp_path, kg_env, **levels):
    project = make_project(tmp_path, kg_env, **levels)
    strategy.apply(strategy.builtins()["entity-graph-agent"], project)
    project = load_project_at(project.harness.root, kg_env)
    assert all(r.ok for r in sync_project(project))
    return project


def item_hash(project, item_id):
    corpus = project.corpus()
    try:
        return next(r.record["hash"] for r in corpus.read_state() if r.item_id == item_id)
    finally:
        corpus.close()


def put_map(project, data, name="a"):
    folder = project.root / entity_map.MAPS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text(json.dumps(data))


def a_map(**over):
    data = {
        "format": entity_map.FORMAT,
        "model": "claude-opus-5-5",
        "run": 7,
        "entities": [
            {
                "kind": "Component",
                "name": "Server",
                "description": "Answers the search requests.",
                "aliases": ["the server"],
                "conf": 0.9,
                "mentions": [{"node": GUIDE, "quote": "the server answers", "line": 3}],
                "code": [
                    {"node": SERVER, "reason": "the class that handles a query", "conf": 0.85},
                    {"node": "dir:app:app", "reason": "its package"},
                ],
            },
            {
                "kind": "Feature",
                "name": "Ranking",
                "description": "Orders the results.",
                "mentions": [{"node": f"{CLUSTER}#demo", "quote": "Repos: app"}],
                "code": [{"node": SEARCH_FILE, "reason": "rank_results lives here", "conf": 0.7}],
            },
            {"kind": "Spaceship", "name": "Rocket", "mentions": [{"node": GUIDE}]},
        ],
        "relations": [
            {
                "src": "Ranking",
                "dst": "the server",
                "keywords": ["search", "order"],
                "description": "The server ranks what it finds.",
                "strength": 8,
                "mentions": [{"node": GUIDE, "quote": "ranked", "line": 5}],
            }
        ],
    }
    data.update(over)
    return data


def graph(project):
    """(nodes: id -> (kind, name, status, props, level), edges: (src, rel, dst) -> (status, conf, props, level))."""
    store = Store.open_file(project.root / "graph.sqlite")
    try:
        b = store.latest_ready()
        live = "tx_from <= ? AND (tx_to IS NULL OR tx_to > ?)"
        nodes = {
            r[0]: (r[1], r[2], r[3], json.loads(r[4]), r[5])
            for r in store.db.execute(
                f"SELECT node_id, kind, name, status, props, label_lvl FROM nodes WHERE {live}", (b, b)
            )
        }
        edges = {
            (r[0], r[1], r[2]): (r[3], r[4], json.loads(r[5]), r[6])
            for r in store.db.execute(
                f"SELECT src, rel, dst, status, conf, props, label_lvl FROM edges WHERE {live}", (b, b)
            )
        }
        return nodes, edges
    finally:
        store.close()


# -- the format ---------------------------------------------------------------------------------------------------


def test_the_strategy_is_built_in_with_its_step_and_the_kinds_of_entity_graph_api_and_two_more():
    found = strategy.builtins()
    agent, api = found["entity-graph-agent"], found["entity-graph-api"]
    assert agent.steps == ("entity_map",) and "entity_map" in strategy.STEPS
    assert set(agent.ontology["node_kinds"]) == set(api.ontology["node_kinds"]) | {"Component", "Feature"}
    assert "entities" not in agent.settings, "the build calls no model"
    assert HUB_RELATIONS[entity_map.IMPLEMENTED_BY] == ("*", "*")


def test_a_map_of_another_format_is_refused_and_a_wrong_entry_is_rejected_alone():
    with pytest.raises(entity_map.EntityMapError):
        entity_map.parse({"format": "evo-entity-map/0", "entities": []})
    with pytest.raises(entity_map.EntityMapError):
        entity_map.parse(b"not json")
    found, rejected = entity_map.parse(
        {
            "format": entity_map.FORMAT,
            "entities": [{"name": ""}, {"name": "Kept", "mentions": [{"quote": "no node"}, {"node": GUIDE}]}],
            "relations": [{"src": "Kept"}],
        }
    )
    assert [e["name"] for e in found["entities"]] == ["Kept"] and found["entities"][0]["kind"] == "Other"
    assert [r["entry"] for r in rejected] == ["entities[0]", "entities[1].mentions[0]", "relations[0]"]


def test_check_drops_what_is_not_there_or_of_the_wrong_kind_and_keeps_the_rest():
    nodes = {GUIDE: "Document", SERVER: "Symbol", "plan:demo": "Plan"}
    found, _ = entity_map.parse(
        {
            "format": entity_map.FORMAT,
            "entities": [
                {
                    "name": "A",
                    "mentions": [{"node": GUIDE}, {"node": "nope"}, {"node": SERVER}],
                    "code": [{"node": SERVER}, {"node": "plan:demo"}],
                },
                {"name": "B", "mentions": [{"node": "hidden"}]},
                {"name": "C", "mentions": [{"node": "plan:demo"}]},
            ],
            "relations": [{"src": "A", "dst": "B"}, {"src": "A", "dst": "C"}, {"src": "A", "dst": "a"}],
        }
    )
    kept, rejected = entity_map.check(found, lambda n: (n, nodes[n]) if n in nodes else None)
    assert [e["name"] for e in kept["entities"]] == ["A", "C"]
    a = kept["entities"][0]
    assert [m["node"] for m in a["mentions"]] == [GUIDE] and [c["node"] for c in a["code"]] == [SERVER]
    assert [(r["src"], r["dst"]) for r in kept["relations"]] == [("A", "C")]
    reasons = {r["entry"]: r["reason"] for r in rejected}
    assert set(reasons) == {
        "entities[0] (A).mentions[1]",
        "entities[0] (A).mentions[2]",
        "entities[0] (A).code[1]",
        "entities[1] (B).mentions[0]",
        "entities[1] (B)",
        "relations[0] (A - B)",
        "relations[2] (A - a)",
    }
    assert "not in the latest build" in reasons["entities[1] (B).mentions[0]"]
    assert "is a Symbol" in reasons["entities[0] (A).mentions[2]"]


# -- the build ----------------------------------------------------------------------------------------------------


def test_the_build_states_the_entities_mentions_relations_and_code_of_a_map(tmp_path, kg_env):
    project = setup(tmp_path, kg_env)
    put_map(project, a_map())
    report = build_project(project)
    assert report.ok, report.errors
    nodes, edges = graph(project)
    server, ranking, rocket = "entity:Component:server", "entity:Feature:ranking", "entity:Other:rocket"
    assert nodes[server][:3] == ("Component", "Server", "extracted")
    assert nodes[server][3]["description"] == "Answers the search requests."
    assert nodes[server][3]["aliases"] == ["the server"]
    assert nodes[rocket][0] == "Other" and nodes[rocket][3]["type"] == "Spaceship"
    mention = edges[(GUIDE, "mentions", server)]
    assert mention[0] == "extracted" and mention[2]["evidence"] == "the server answers" and mention[2]["line"] == 3
    assert (f"{CLUSTER}#demo", "mentions", ranking) in edges
    impl = edges[(server, "implemented_by", SERVER)]
    assert impl[0] == "extracted" and impl[1] == pytest.approx(0.85)
    assert impl[2] == {
        "reason": "the class that handles a query",
        "conf": 0.85,
        "run": 7,
        "model": "claude-opus-5-5",
        "level": "Symbol",
    }
    assert edges[(server, "implemented_by", "dir:app:app")][2]["level"] == "Directory"
    assert (ranking, "implemented_by", SEARCH_FILE) in edges
    rel = edges[(server, "relates_to", ranking)]  # its ends in order
    assert rel[0] == "extracted"
    assert rel[2]["keywords"] == "search, order" and rel[2]["strength"] == 8 and rel[2]["evidence"] == "ranked"
    assert edges[(server, "refers_to", SERVER)][0] == "extracted", "the name match stays"
    touching = [v for k, v in edges.items() if k[0].startswith("entity:") or k[2].startswith("entity:")]
    assert {v[0] for v in touching} == {"extracted"}


def test_coverage_counts_what_the_maps_gave(tmp_path, kg_env):
    project = setup(tmp_path, kg_env)
    put_map(project, a_map())
    cov = build_project(project).coverage
    found = cov["entity_map"]
    assert found["maps"] == 1 and found["runs"] == [7] and found["model"] == "claude-opus-5-5"
    assert found["nodes"] == 3 and found["relations"] == 1
    assert found["code_links"] == {"Directory": 1, "File": 1, "Symbol": 1}
    assert found["name_links"] == 1 and found["mentions"] == 3 and found["stale"] == 0
    assert found["cited_items"] == 2 and found["uncited_items"] >= 1
    assert GUIDE not in found["uncited_samples"] and "app:file:docs/old.md" in found["uncited_samples"]
    assert cov["entities"] == {"enabled": True, "style": "agent", "model": "claude-opus-5-5", "nodes": 3, "edges": 1}


def test_a_citation_of_an_item_that_changed_since_it_was_read_is_stale(tmp_path, kg_env):
    project = setup(tmp_path, kg_env)
    fresh = item_hash(project, GUIDE)
    data = a_map()
    data["entities"][0]["mentions"][0]["hash"] = "sha256:what-the-agent-read"
    data["entities"][1]["mentions"][0]["hash"] = item_hash(project, CLUSTER)
    data["entities"][2]["mentions"][0]["hash"] = fresh
    put_map(project, data)
    report = build_project(project)
    _, edges = graph(project)
    assert edges[(GUIDE, "mentions", "entity:Component:server")][2]["stale"] is True
    assert "stale" not in edges[(f"{CLUSTER}#demo", "mentions", "entity:Feature:ranking")][2]
    assert "stale" not in edges[(GUIDE, "mentions", "entity:Other:rocket")][2]
    assert report.coverage["entity_map"]["stale"] == 1 and report.coverage["entity_map"]["stale_items"] == [GUIDE]


def test_two_maps_naming_one_entity_make_one_node_and_a_missing_node_is_rejected(tmp_path, kg_env):
    project = setup(tmp_path, kg_env)
    put_map(project, a_map())
    second = {
        "format": entity_map.FORMAT,
        "model": "claude-sonnet-5-5",
        "run": 8,
        "entities": [
            {"kind": "Component", "name": "servers", "mentions": [{"node": "app:file:docs/old.md"}]},
            {"kind": "Component", "name": "Ghost", "mentions": [{"node": "app:file:docs/gone.md"}]},
        ],
    }
    put_map(project, second, "b")
    cov = build_project(project).coverage["entity_map"]
    nodes, edges = graph(project)
    assert ("app:file:docs/old.md", "mentions", "entity:Component:server") in edges
    assert not [n for n in nodes if n.startswith("entity:Component:ghost")]
    assert cov["maps"] == 2 and cov["runs"] == [7, 8] and cov["merged"] == 1 and cov["rejected"] == 2


def test_the_label_joins_the_cited_items_and_the_code(tmp_path, kg_env):
    project = setup(tmp_path, kg_env, level="public", app_level="customer")
    data = {
        "format": entity_map.FORMAT,
        "entities": [
            {
                "kind": "Feature",
                "name": "Demo",
                "mentions": [{"node": f"{CLUSTER}#demo"}],
                "code": [{"node": SERVER, "reason": "serves it"}],
            }
        ],
    }
    put_map(project, data)
    assert build_project(project).ok
    nodes, edges = graph(project)
    public, customer = nodes[CLUSTER][4], nodes[SERVER][4]
    assert public < customer
    assert nodes["entity:Feature:demo"][4] == public, "the entity reads at the level of the items it cites"
    assert edges[("entity:Feature:demo", "implemented_by", SERVER)][3] == customer, "and its code link at the code's"


def test_a_new_map_is_a_new_build_and_no_map_keeps_the_digest(tmp_path, kg_env):
    project = setup(tmp_path, kg_env)
    assert build_project(project).ok
    assert unchanged(project) is not None
    put_map(project, a_map())
    assert unchanged(project) is None
    assert build_project(project).ok
    assert unchanged(project) is not None


def test_without_the_step_the_maps_are_not_read(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    assert all(r.ok for r in sync_project(project))
    put_map(project, a_map())
    report = build_project(project)
    assert report.ok and "entity_map" not in report.coverage
    nodes, _ = graph(project)
    assert not [n for n in nodes if n.startswith("entity:")]
