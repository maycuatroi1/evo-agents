"""The entities step: off without a strategy that turns it on, local or sink-cleared models only, answers cut to the
ontology, results cached, a ceiling per build."""

import json
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from evo_agents.kg import entities, strategy
from evo_agents.kg import settings as kg_settings
from evo_agents.kg.build import build_project, unchanged, verify_build
from evo_agents.kg.project import load_project_at
from evo_agents.kg.store import Store
from evo_agents.kg.sync import sync_project
from tests.kg.projects import make_project, write

MODEL = """import json, sys
req = json.load(sys.stdin)
open(sys.argv[1], "a").write("call\\n")
name = "Search" if "Intro" in req["text"] else "Obsolete"
print(json.dumps({
    "nodes": [{"kind": "Concept", "name": name, "evidence": "from the text", "conf": 0.8},
              {"kind": "Concept", "name": "Server", "evidence": "x"},
              {"kind": "Banana", "name": "Nope"}],
    "edges": [{"src": name, "rel": "depends_on", "dst": "Server"}, {"src": name, "rel": "eats", "dst": "Server"}],
    "usage": {"input_tokens": 100, "output_tokens": 20, "cost": 0.5},
}))
"""


def calls(log: Path) -> int:
    return len(log.read_text().splitlines()) if log.exists() else 0


def setup(tmp_path, kg_env, *, sink=None, extra=None, enable=True, level="internal"):
    project = make_project(tmp_path, kg_env)
    script, log = tmp_path / "model.py", tmp_path / "calls.log"
    script.write_text(MODEL)
    mapping = {
        "model": "fake-1",
        "command": [sys.executable, str(script), str(log)],
        "node_kinds": ["Concept", "Banana"],
        "relations": ["depends_on", "eats"],
        "items": ["markdown"],
    }
    if sink:
        mapping["sink"] = sink
    mapping |= extra or {}
    path = project.knowledge_path
    data = yaml.safe_load(path.read_text())
    data["policy"]["sinks"] += [
        {"id": "llm", "kind": "llm-backend", "clearance": {"level": "internal"}},
        {"id": "llm-low", "kind": "llm-backend", "clearance": {"level": "public"}},
    ]
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    project = load_project_at(project.harness.root, kg_env)
    data = {
        "version": 1,
        "name": "ents",
        "knowledge": {"ontology": {"node_kinds": ["Concept"]}},
        "settings": {"entities": mapping},
        "steps": ["entities"] if enable else [],
    }
    chosen = project.harness.root / "strategies" / "ents.yaml"
    write(chosen, yaml.safe_dump(data))
    strategy.apply(strategy.load_file(chosen), project)
    project = load_project_at(project.harness.root, kg_env)
    assert all(r.ok for r in sync_project(project))
    return project, log


def entity_nodes(project):
    store = Store.open_file(project.root / "graph.sqlite")
    try:
        b = store.latest_ready()
        rows = store.db.execute(
            "SELECT node_id, kind, status, props FROM nodes WHERE node_id LIKE 'entity:%'"
            " AND tx_from <= ? AND (tx_to IS NULL OR tx_to > ?)",
            (b, b),
        )
        return [tuple(r) for r in rows.fetchall()]
    finally:
        store.close()


def test_without_a_strategy_that_enables_it_no_model_is_called(tmp_path, kg_env):
    project, log = setup(tmp_path, kg_env, enable=False)
    report = build_project(project)
    assert report.ok, report.errors
    assert calls(log) == 0 and entity_nodes(project) == []
    assert report.coverage["entities"]["enabled"] is False
    assert "does not enable" in report.coverage["entities"]["reason"]


def test_a_project_with_no_entities_mapping_says_nothing_of_the_step(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    report = build_project(project)
    assert report.ok and "entities" not in report.coverage


def test_a_local_model_states_entities_cut_to_the_ontology_and_cached(tmp_path, kg_env):
    project, log = setup(tmp_path, kg_env)
    report = build_project(project)
    assert report.ok, report.errors
    found = entity_nodes(project)
    assert {r[0] for r in found} >= {"entity:Concept:search", "entity:Concept:server", "entity:Concept:obsolete"}
    assert {r[1] for r in found} == {"Concept"} and {r[2] for r in found} == {"extracted"}
    assert all(json.loads(r[3])["model"] == "fake-1" for r in found)
    cov = report.coverage["entities"]
    assert cov["local"] is True and cov["calls"] == calls(log) == 3
    assert cov["tokens_in"] == 300 and cov["tokens_out"] == 60 and cov["cost"] == 1.5
    assert cov["rejected"] >= 4, "Banana is not in the ontology and eats is not a relation"
    assert "Banana" in cov["rejected_config"]
    again = build_project(project)
    assert calls(log) == 3, "an unchanged item costs no second call"
    assert again.ok


def test_a_sink_that_does_not_clear_the_label_gets_nothing_and_coverage_says_why(tmp_path, kg_env):
    project, log = setup(tmp_path, kg_env, sink="llm-low")
    report = build_project(project)
    cov = report.coverage["entities"]
    assert calls(log) == 0 and cov["calls"] == 0 and cov["refused_count"] >= 2
    assert "above the clearance of sink 'llm-low'" in cov["refused"][0]["reason"]
    assert entity_nodes(project) == []


def test_a_sink_that_clears_the_label_is_used_and_a_wrong_sink_is_refused(tmp_path, kg_env):
    project, log = setup(tmp_path, kg_env, sink="llm")
    assert build_project(project).coverage["entities"]["calls"] == 3
    project, log = setup(tmp_path / "b", kg_env, sink="agent")
    cov = build_project(project).coverage["entities"]
    assert cov["calls"] == 0 and "not llm-backend" in cov["refused"][0]["reason"]


def test_the_ceiling_of_a_build_leaves_the_rest_for_the_next(tmp_path, kg_env):
    project, log = setup(tmp_path, kg_env, extra={"max_calls": 1})
    report = build_project(project)
    cov = report.coverage["entities"]
    assert cov["calls"] == 1 and cov["skipped"] == 2 and "max_calls 1" in cov["stopped"]
    assert unchanged(project) is None, "a build that left items is not the build to skip"
    path = project.root / "settings.json"
    data = json.loads(path.read_text())
    data["entities"]["max_calls"] = 5
    path.write_text(json.dumps(data))
    after = build_project(project).coverage["entities"]
    assert after["calls"] == 2 and after["cache_hits"] == 1 and after["skipped"] == 0
    assert calls(log) == 3


def test_the_cost_ceiling_stops_too(tmp_path, kg_env):
    project, log = setup(tmp_path, kg_env, extra={"max_cost": 0.4})
    cov = build_project(project).coverage["entities"]
    assert cov["calls"] == 1 and "max_cost" in cov["stopped"]


def test_calls_at_once_give_the_answers_of_calls_one_by_one_and_are_memoized(tmp_path, kg_env):
    project, log = setup(tmp_path, kg_env, extra={"jobs": 3})
    report = build_project(project)
    cov = report.coverage["entities"]
    assert report.ok and cov["calls"] == calls(log) == 3 and cov["cache_hits"] == 0 and cov["skipped"] == 0
    assert cov["cost"] == 1.5
    found = entity_nodes(project)
    assert {r[0] for r in found} >= {"entity:Concept:search", "entity:Concept:server", "entity:Concept:obsolete"}
    assert build_project(project).ok
    assert calls(log) == 3, "the answers of calls at once are in the memo"


def test_calls_at_once_stop_at_the_ceiling_and_leave_the_rest(tmp_path, kg_env):
    project, log = setup(tmp_path, kg_env, extra={"jobs": 2, "max_calls": 1})
    cov = build_project(project).coverage["entities"]
    assert cov["calls"] == 1 == calls(log) and cov["skipped"] == 2 and "max_calls 1" in cov["stopped"]


def test_verifying_a_build_never_calls_the_model(tmp_path, kg_env):
    project, log = setup(tmp_path, kg_env)
    report = build_project(project)
    before = calls(log)
    for cold in (False, True):
        got = verify_build(project, report.content_hash, cold=cold)
        assert got["match"], got
    assert calls(log) == before


def test_a_failing_model_is_counted_and_the_build_still_succeeds(tmp_path, kg_env):
    project, log = setup(tmp_path, kg_env, extra={"command": [sys.executable, "-c", "import sys; sys.exit(3)"]})
    report = build_project(project)
    cov = report.coverage["entities"]
    assert report.ok and cov["failed"] == 3 and cov["errors"][0]["error"].startswith("the model command exited 3")


@pytest.mark.parametrize(
    "value, message",
    [
        ({"model": "m", "node_kinds": ["A"], "url": "https://api.example.test/v1"}, "name the llm-backend sink"),
        ({"model": "m", "node_kinds": ["A"]}, "needs a command"),
        ({"model": "m", "command": ["x"]}, "node_kinds"),
        ({"model": "m", "node_kinds": ["A"], "command": ["x"], "max_calls": 0}, "positive"),
        ({"model": "m", "node_kinds": ["A"], "command": ["x"], "color": 1}, "unknown keys"),
        ({"model": "m", "node_kinds": ["A"], "command": ["x"], "url": "http://localhost:1"}, "not both"),
    ],
)
def test_a_mapping_with_a_mistake_is_refused(value, message):
    with pytest.raises(kg_settings.SettingsError, match=message):
        kg_settings.of({"entities": value})


def test_a_remote_url_through_a_sink_is_accepted_and_its_library_is_an_extra():
    got = kg_settings.of(
        {"entities": {"model": "m", "node_kinds": ["A"], "url": "https://x.example.test/v1", "sink": "s"}}
    )
    assert got.entities["sink"] == "s"
    data = tomllib.load(open(Path(__file__).parents[2] / "pyproject.toml", "rb"))["project"]
    assert "openai" in data["optional-dependencies"]["entities"][0]
    assert sorted(data["dependencies"]) == ["PyYAML>=6.0", "tzdata>=2024.1"]


def test_the_endpoint_model_reads_usage_and_names_a_missing_library(monkeypatch):
    config = {"model": "m", "url": "http://localhost:9/v1", "node_kinds": ["Concept"]}

    class Msg:
        content = 'text {"nodes": [{"kind": "Concept", "name": "A"}]}'

    class Got:
        choices = [type("C", (), {"message": Msg})]
        usage = type("U", (), {"prompt_tokens": 7, "completion_tokens": 3})

    client = type(
        "K", (), {"chat": type("H", (), {"completions": type("P", (), {"create": staticmethod(lambda **k: Got)})})}
    )
    monkeypatch.setattr(entities, "CLIENT_FACTORY", lambda c: client)
    got = entities.call_endpoint(config, "t", ["Concept"], [])
    assert got["usage"] == {"input_tokens": 7, "output_tokens": 3}
    monkeypatch.setitem(sys.modules, "openai", None)
    monkeypatch.setattr(entities, "CLIENT_FACTORY", entities._openai_client)
    with pytest.raises(entities.EntitiesError, match="evo-agents\\[entities\\]"):
        entities.call_endpoint(config, "t", ["Concept"], [])
