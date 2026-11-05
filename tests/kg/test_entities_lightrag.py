"""The lightrag style of the entities step: entities of open types with a description, relations with keywords, a
description and a strength, each with its item, quote and line; through a command, an endpoint and the relay; the
closed style and the multi-hop strategies as they were."""

import json
import subprocess
import sys

import pytest
import yaml

from evo_agents.hub import kg_entity_relay
from evo_agents.kg import claude_code, entities, init_run, review, strategy
from evo_agents.kg.build import build_project
from evo_agents.kg.project import load_project_at
from evo_agents.kg.protocol import canonical_json, sha256
from evo_agents.kg.store import Store
from evo_agents.kg.sync import sync_project
from tests.kg.projects import make_project, write

KINDS = ["Actor", "Concept", "Artifact", "Other"]
MODEL = """import json, pathlib, sys, uuid
req = json.load(sys.stdin)
pathlib.Path(sys.argv[1], uuid.uuid4().hex).write_text(req.get("style") or "closed")
if req.get("style") != "lightrag":
    print(json.dumps({"nodes": [], "edges": []}))
    raise SystemExit
print(json.dumps({
    "nodes": [{"name": "Search", "type": "concept", "description": "The app's search.", "evidence": "See"},
              {"name": "Server", "type": "Software", "description": "What runs the app.",
               "evidence": "Run the server from app/server.py"},
              {"name": "Ann", "type": "Person", "description": "Owner.", "evidence": "nowhere"}],
    "edges": [{"src": "Search", "dst": "Server", "keywords": ["runs on", "hosting"], "description": "Search runs on it.",
               "strength": 8, "evidence": "Run the server"},
              {"src": "Search", "dst": "Nobody", "keywords": "x"}],
    "usage": {"input_tokens": 100, "output_tokens": 40, "cost": 0.01},
}))
"""


def setup(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    script, log = tmp_path / "model.py", tmp_path / "calls"
    script.write_text(MODEL)
    log.mkdir()
    mapping = {
        "model": "fake-1",
        "command": [sys.executable, str(script), str(log)],
        "style": "lightrag",
        "node_kinds": KINDS,
        "relations": ["relates_to"],
        "items": ["markdown"],
    }
    data = {
        "version": 1,
        "name": "graph",
        "knowledge": {"ontology": {"node_kinds": KINDS, "relations": ["relates_to"]}},
        "settings": {"entities": mapping},
        "steps": ["entities"],
    }
    chosen = project.harness.root / "strategies" / "graph.yaml"
    write(chosen, yaml.safe_dump(data))
    strategy.apply(strategy.load_file(chosen), project)
    project = load_project_at(project.harness.root, kg_env)
    assert all(r.ok for r in sync_project(project))
    return project, log


def rows(project, table: str, where: str) -> list[tuple]:
    store = Store.open_file(project.root / "graph.sqlite")
    try:
        b = store.latest_ready()
        cols = "node_id, kind, props" if table == "nodes" else "src, rel, dst, props"
        found = store.db.execute(
            f"SELECT {cols} FROM {table} WHERE {where} AND tx_from <= ? AND (tx_to IS NULL OR tx_to > ?)", (b, b)
        )
        return [tuple(r) for r in found.fetchall()]
    finally:
        store.close()


def test_a_lightrag_model_gives_typed_described_entities_and_keyworded_relations_with_their_source(tmp_path, kg_env):
    project, log = setup(tmp_path, kg_env)
    report = build_project(project)
    assert report.ok, report.errors
    assert {p.read_text() for p in log.iterdir()} == {"lightrag"}, "the command is asked in the lightrag style"
    nodes = {nid: (kind, json.loads(props)) for nid, kind, props in rows(project, "nodes", "node_id LIKE 'entity:%'")}
    kind, search = nodes["entity:Concept:search"]
    assert kind == "Concept" and search["description"] == "The app's search." and "type" not in search
    assert search["item"] and search["model"] == "fake-1" and search["evidence"] == "See"
    kind, server = nodes["entity:Other:server"]
    assert kind == "Other" and server["type"] == "Software", "a type outside the suggestion is Other, its word kept"
    assert server["line"] == 7, "the quote's line in the item"
    kind, ann = nodes["entity:Actor:ann"]
    assert kind == "Actor" and ann["type"] == "Person" and "line" not in ann, "Person is Actor; no line without quote"
    found = rows(project, "edges", "rel = 'relates_to'")
    assert [(s, d) for s, _, d, _ in found] == [("entity:Concept:search", "entity:Other:server")]
    props = json.loads(found[0][3])
    assert props["keywords"] == "runs on, hosting" and props["description"] == "Search runs on it."
    assert props["strength"] == 8.0 and props["evidence"] == "Run the server" and props["line"] == 7
    cov = report.coverage["entities"]
    assert cov["tokens_out"] == 40 * cov["calls"] and cov["rejected"] >= 1, "the edge to an unknown name is dropped"
    calls = len(list(log.iterdir()))
    assert build_project(project).ok and len(list(log.iterdir())) == calls, "memoized by the item's digest"


def test_the_closed_style_keeps_its_prompt_digest_and_answers():
    config = {"model": "m", "node_kinds": ["Concept"], "relations": ["uses"], "command": ["x"]}
    before = sha256(
        canonical_json(
            {"model": "m", "node_kinds": ["Concept"], "relations": ["uses"]}
            | {"max_chars": entities.DEFAULT_MAX_CHARS, "version": entities.VERSION}
        )
    )[:16]
    assert entities.config_digest(config, ["Concept"], ["uses"]) == before, "a closed mapping's memo still reads"
    assert entities.config_digest({**config, "style": "lightrag"}, ["Concept"], ["uses"]) != before
    closed = entities.prompt("T", ["Concept"], ["uses"])
    assert (
        closed == entities.prompt("T", ["Concept"], ["uses"], None) and '"conf"' in closed and "keywords" not in closed
    )
    light = entities.prompt("T", ["Concept", "Other"], ["relates_to"], "lightrag")
    assert "keywords" in light and "strength" in light and "Other" in light and "relates_to" not in light
    answer = {
        "nodes": [{"kind": "Concept", "name": "A", "description": "d"}, {"kind": "Concept", "name": "B"}],
        "edges": [{"src": "A", "rel": "uses", "dst": "B", "keywords": "k"}],
    }
    got = entities._clean(answer, {"Concept"}, ["uses"])
    assert set(got["nodes"][0]) == {"kind", "name", "conf", "evidence"} and set(got["edges"][0]) == {
        "src",
        "rel",
        "dst",
        "evidence",
    }, "a closed answer gets no lightrag field"


def test_the_built_in_strategies_and_the_curator_name_the_lightrag_ones():
    found = strategy.builtins()
    for name, path in (("entity-graph", "command"), ("entity-graph-api", "url")):
        mapping = entities.check(found[name].settings["entities"])
        assert mapping["style"] == "lightrag" and path in mapping and mapping["sink"] == "llm"
        assert mapping["relations"] == ["relates_to"] and mapping["node_kinds"][-1] == "Other"
        assert "entities" in found[name].steps and "prose" in found[name].suits
    for name in ("multi-hop", "multi-hop-api"):
        assert "style" not in found[name].settings["entities"], "the closed strategies are as they were"
    assert "entity-graph-api" in init_run.ENTITIES_HELP
    assert "relates_to" in review.CLAIM_SKIP, "a lightrag relation has no predicate to conflict on"


def test_the_endpoint_and_claude_code_are_asked_in_the_style_of_the_mapping(monkeypatch):
    sent, bodies = [], []

    class Msg:
        content = '{"nodes": [{"name": "A", "type": "Concept"}], "edges": []}'

    class Got:
        choices = [type("C", (), {"message": Msg})]
        usage = type("U", (), {"prompt_tokens": 7, "completion_tokens": 3})

    def create(**kwargs):
        sent.append(kwargs["messages"][0]["content"])
        bodies.append(kwargs.get("extra_body"))
        return Got

    client = type("K", (), {"chat": type("H", (), {"completions": type("P", (), {"create": staticmethod(create)})})})
    monkeypatch.setattr(entities, "CLIENT_FACTORY", lambda c, key=None: client)
    config = {"model": "m", "url": "http://localhost:9/v1", "node_kinds": KINDS, "relations": ["relates_to"]}
    entities.call_endpoint({**config, "style": "lightrag"}, "t", KINDS, ["relates_to"])
    entities.call_endpoint(config, "t", KINDS, ["relates_to"])
    assert "keywords" in sent[0] and "keywords" not in sent[1] and bodies[:2] == [None, None]
    entities.call_endpoint({**config, "reasoning": {"enabled": False}}, "t", KINDS, ["relates_to"])
    assert bodies[2] == {"reasoning": {"enabled": False}}, "the reasoning of the mapping reaches the endpoint"
    with pytest.raises(entities.EntitiesError, match="reasoning"):
        entities.check({"model": "m", "node_kinds": ["A"], "command": ["x"], "reasoning": {"enabled": False}})
    with pytest.raises(entities.EntitiesError, match="style"):
        entities.check({"model": "m", "node_kinds": ["A"], "command": ["x"], "style": "open"})

    given = []

    def runner(args, **kwargs):
        given.append(kwargs["input"])
        result = {"result": Msg.content, "usage": {"input_tokens": 1, "output_tokens": 2}, "subtype": "success"}
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(result), stderr="")

    env = {claude_code.BINARY_ENV: sys.executable}
    request = {"text": "t", "node_kinds": KINDS, "relations": ["relates_to"], "model": "m", "style": "lightrag"}
    got = claude_code.answer(request, env=env, runner=runner)
    claude_code.answer({k: v for k, v in request.items() if k != "style"}, env=env, runner=runner)
    assert "keywords" in given[0] and "keywords" not in given[1] and got["nodes"][0]["type"] == "Concept"


def test_the_relay_keeps_the_style_of_a_request_and_adds_none_to_a_closed_one(tmp_path):
    base = {"key": "k1", "item": "i", "text": "t", "node_kinds": ["A"], "relations": [], "model": "m"}
    kg_entity_relay.write_requests(tmp_path, [base, {**base, "key": "k2", "style": "lightrag"}])
    found = kg_entity_relay.parse_requests((tmp_path / kg_entity_relay.REQUESTS_FILE).read_bytes())
    assert "style" not in found[0] and found[1]["style"] == "lightrag"
