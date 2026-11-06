"""Strategy entity-graph-all: the entities step over every text item and the entity_map step in one build, and the
ceiling stop after which the next build goes on at once (docs/kg-strategies.md, "entity-graph-all")."""

from __future__ import annotations

import json
import sys

import yaml

from evo_agents.kg import entity_map, strategy
from evo_agents.kg.build import build_project
from evo_agents.kg.entities import config_digest
from evo_agents.kg.entity_mapping import CEILING, goes_on
from evo_agents.kg.profile import class_of
from evo_agents.kg.project import load_project_at
from evo_agents.kg.store import Store
from evo_agents.kg.sync import sync_project
from tests.kg.projects import make_project, write

GUIDE = "app:file:docs/guide.md"
SERVER = "symbol:app:app/server.py::Server"

MODEL = """import json, sys
req = json.load(sys.stdin)
print(json.dumps({
    "nodes": [{"name": "Search", "type": "Concept", "description": "Finding things.", "evidence": "search"}],
    "edges": [],
    "usage": {"input_tokens": 10, "output_tokens": 5, "cost": 0.0},
}))
"""


def mapping_of(name: str) -> dict:
    return strategy.builtins()[name].settings["entities"]


def test_the_strategy_runs_both_steps_over_every_text_class_with_the_digest_of_entity_graph_api():
    found = strategy.builtins()["entity-graph-all"]
    assert found.steps == ("entities", "entity_map")
    assert set(found.ontology["node_kinds"]) == set(strategy.builtins()["entity-graph-agent"].ontology["node_kinds"])
    classes = {class_of(kind) for kind in mapping_of("entity-graph-all")["items"]}
    assert classes == {"prose", "structured", "conversation", "history"}, "code is the map's, binaries hold no text"
    mine, api = mapping_of("entity-graph-all"), mapping_of("entity-graph-api")
    assert config_digest(mine, mine["node_kinds"], mine["relations"]) == config_digest(
        api, api["node_kinds"], api["relations"]
    ), "a project moving from entity-graph-api keeps the answers its memo holds"


def test_goes_on_only_after_a_ceiling_stop_that_read_something():
    def cov(stopped, calls=3, failed=0):
        return {"entities": {"stopped": stopped, "calls": calls, "failed": failed}}

    assert goes_on(cov(f"{CEILING} (max_tokens 400000); the rest waits for a later build"))
    assert not goes_on(cov(f"{CEILING} (max_calls 0); the rest waits", calls=0)), "no headway: no loop"
    assert not goes_on(cov(f"{CEILING} (max_cost 5.0); the rest waits", calls=2, failed=2))
    assert not goes_on(cov("the model stopped the step (limit); the rest waits for a later build"))
    assert not goes_on(cov(None)) and not goes_on({}) and not goes_on(None)


def test_one_build_holds_the_model_entities_and_the_map_entities(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    script = tmp_path / "model.py"
    script.write_text(MODEL)
    path = project.knowledge_path
    data = yaml.safe_load(path.read_text())
    data["policy"]["sinks"] += [{"id": "llm", "kind": "llm-backend", "clearance": {"level": "internal"}}]
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    chosen = yaml.safe_load((strategy.BUILTIN_DIR / "entity-graph-all.yaml").read_text(encoding="utf-8"))
    local = {
        k: v
        for k, v in mapping_of("entity-graph-all").items()
        if k not in ("url", "key_env", "sink", "price", "reasoning")
    }
    chosen["settings"]["entities"] = {**local, "model": "fake-1", "command": [sys.executable, str(script)]}
    chosen["name"] = "all-local"
    target = project.harness.root / "strategies" / "all-local.yaml"
    write(target, yaml.safe_dump(chosen))
    strategy.apply(strategy.load_file(target), project)
    project = load_project_at(project.harness.root, kg_env)
    assert all(r.ok for r in sync_project(project))
    folder = project.root / entity_map.MAPS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "a.json").write_text(
        json.dumps(
            {
                "format": entity_map.FORMAT,
                "entities": [
                    {"kind": "Component", "name": "Server", "mentions": [{"node": GUIDE}], "code": [{"node": SERVER}]}
                ],
            }
        )
    )
    report = build_project(project)
    assert report.ok, report.errors
    assert report.coverage["entities"]["calls"] > 0 and report.coverage["entity_map"]["nodes"] == 1
    store = Store.open_file(project.root / "graph.sqlite")
    try:
        b = store.latest_ready()
        ids = {
            r[0]
            for r in store.db.execute(
                "SELECT node_id FROM nodes WHERE tx_from <= ? AND (tx_to IS NULL OR tx_to > ?)", (b, b)
            )
        }
    finally:
        store.close()
    assert {"entity:Concept:search", "entity:Component:server"} <= ids
