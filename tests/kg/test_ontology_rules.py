"""The rules an ontology states (``evo_agents.kg.ontology_rules``): what the schema accepts of them, and the check
of a snapshot that finds wrong endpoints, too many targets or sources, cycles, duplicate keys and missing props."""

import json
import re
from pathlib import Path

import yaml

from evo_agents.kg import review
from evo_agents.kg.ontology_rules import RelationRule, check, review_items, rules_of
from evo_agents.kg.schema import known_kinds, known_relations, validate_ontology
from evo_agents.kg.snapshot import Snapshot
from evo_agents.kg.snapshot_text import TextIndex

ONTOLOGY = {
    "node_kinds": [
        "Concept",
        {"name": "Service", "keys": ["slug", ["repo", "path"]], "required_props": ["owner"]},
        {"name": "File", "required_props": ["path"]},  # a hub kind refined by rules only
    ],
    "relations": [
        {"name": "owns", "domain": ["Service"], "range": ["File"], "cardinality": "one_to_many"},
        {"name": "runs_on", "domain": ["Service"], "cardinality": "many_to_one"},
        {"name": "depends_on", "acyclic": True},  # a hub relation refined by rules only
        "relates_to",
    ],
}


def node(nid: str, kind: str, **props) -> tuple:
    return (nid, kind, nid, json.dumps(props), "declared", 1.0, 0, 0, "U", "[]", None)


def edge(src: str, rel: str, dst: str) -> tuple:
    return (f"{src}|{rel}|{dst}", src, rel, dst, "{}", "declared", 1.0, 0, 0, "U", None)


def snapshot(nodes: list[tuple], edges: list[tuple]) -> Snapshot:
    return Snapshot.from_rows("hash", nodes, edges, TextIndex.build({}, {}))


def test_the_step_verify_and_plain_lists_stay_valid():
    rule = {"name": "custom_rel", "cardinality": "many_to_one", "domain": ["Concept"], "range": ["File"]}
    assert validate_ontology({"relations": [rule]}) == []
    assert validate_ontology({"node_kinds": ["Screen"], "relations": ["shown_on"]}) == []
    assert validate_ontology(ONTOLOGY) == []
    assert known_kinds(ONTOLOGY) >= {"Concept", "Service", "File"}
    assert known_relations(ONTOLOGY) >= {"owns", "runs_on", "relates_to"}


def test_a_hub_name_bare_or_with_a_new_shape_is_still_refused():
    issues = validate_ontology(
        {
            "node_kinds": ["File", {"name": "Document", "description": "mine"}],
            "relations": [
                {"name": "calls"},
                {"name": "calls", "domain": ["Concept"]},
                {"name": "imports", "acyclic": True},
            ],
        }
    )
    assert [(i.path, i.message) for i in issues] == [
        ("node_kinds[0]", "'File' is a hub kind; extend it with properties instead"),
        ("node_kinds[1]", "'Document' is a hub kind; extend it with properties instead"),
        ("relations[0]", "'calls' is a hub relation"),
        ("relations[1]", "'calls' is a hub relation"),
    ]


def test_malformed_rules_are_named():
    issues = validate_ontology(
        {
            "node_kinds": [{"name": "Service", "keys": "slug", "required_props": [1]}],
            "relations": [{"name": "owns", "cardinality": "lots", "acyclic": "yes", "domain": "Service"}],
        }
    )
    assert sorted(i.path for i in issues) == [
        "node_kinds[0].keys",
        "node_kinds[0].required_props",
        "relations[0].acyclic",
        "relations[0].cardinality",
        "relations[0].domain",
    ]


def test_rules_of_reads_only_what_states_a_rule():
    rules = rules_of(ONTOLOGY)
    assert set(rules.relations) == {"owns", "runs_on", "depends_on"}
    assert rules.relations["owns"] == RelationRule("owns", frozenset({"Service"}), frozenset({"File"}), "one_to_many")
    assert rules.kinds["Service"].keys == (("slug",), ("repo", "path"))
    assert not rules_of({"node_kinds": ["Screen"], "relations": ["shown_on"]})
    assert not rules_of(None)


def test_check_finds_each_kind_of_violation():
    nodes = [
        node("svc:a", "Service", slug="a", owner="ops"),
        node("svc:b", "Service", slug="a", owner="ops"),  # the slug of svc:a
        node("svc:c", "Service", repo="r", path="p"),  # no owner
        node("svc:d", "Service", repo="r", path="p", owner="ops"),  # the repo and path of svc:c
        node("file:x", "File", path="x"),
        node("file:y", "File"),  # no path
        node("concept:z", "Concept"),
        node("host:1", "Concept"),
        node("host:2", "Concept"),
    ]
    edges = [
        edge("svc:a", "owns", "file:x"),
        edge("svc:b", "owns", "file:x"),  # file:x has two owners: one_to_many
        edge("concept:z", "owns", "file:y"),  # a Concept owns nothing: wrong source
        edge("svc:a", "runs_on", "host:1"),
        edge("svc:a", "runs_on", "host:2"),  # two hosts: many_to_one
        edge("svc:b", "runs_on", "host:1"),
        edge("svc:a", "depends_on", "svc:b"),
        edge("svc:b", "depends_on", "svc:c"),
        edge("svc:c", "depends_on", "svc:a"),  # a cycle of three
        edge("svc:d", "depends_on", "svc:d"),  # a loop
        edge("svc:d", "relates_to", "svc:d"),  # no rule
    ]
    found = check(snapshot(nodes, edges), rules_of(ONTOLOGY))
    by_rule = {}
    for v in found:
        by_rule.setdefault(v.rule, []).append(v)
    assert [v.nodes for v in by_rule["wrong_endpoint"]] == [("concept:z", "file:y")]
    assert by_rule["wrong_endpoint"][0].detail["wrong"] == [{"end": "source", "kind": "Concept"}]
    cardinality = sorted((v.detail["relation"], v.nodes) for v in by_rule["cardinality"])
    assert cardinality == [("owns", ("file:x", "svc:a", "svc:b")), ("runs_on", ("svc:a", "host:1", "host:2"))]
    assert sorted(v.nodes for v in by_rule["cycle"]) == [("svc:a", "svc:b", "svc:c"), ("svc:d",)]
    assert sorted(v.nodes for v in by_rule["duplicate_key"]) == [("svc:a", "svc:b"), ("svc:c", "svc:d")]
    assert sorted(v.nodes for v in by_rule["missing_prop"]) == [("file:y",), ("svc:c",)]
    assert found == check(snapshot(list(reversed(nodes)), list(reversed(edges))), rules_of(ONTOLOGY))


def test_a_graph_within_the_rules_and_a_status_filter_find_nothing():
    nodes = [node("svc:a", "Service", slug="a", owner="ops"), node("file:x", "File", path="x")]
    edges = [edge("svc:a", "owns", "file:x"), edge("svc:a", "depends_on", "file:x")]
    assert check(snapshot(nodes, edges), rules_of(ONTOLOGY)) == []
    wrong = [*nodes, node("concept:z", "Concept")]
    assert check(snapshot(wrong, [*edges, edge("concept:z", "owns", "file:x")]), rules_of(ONTOLOGY), {"parsed"}) == []


def test_violations_are_review_items_with_stable_fingerprints():
    nodes = [node("svc:a", "Service", slug="a"), node("svc:b", "Service", slug="a", owner="ops")]
    found = check(snapshot(nodes, []), rules_of(ONTOLOGY))
    items = review_items(found, {"svc:a": "c1"}.get)
    assert {i["rule"] for i in items} == {"duplicate_key", "missing_prop"}
    for item in items:
        assert set(item) == {"fingerprint", "rule", "cluster", "evidence", "clusters", "action", "detail"}
        assert re.fullmatch(r"[a-z_]+", item["action"]), "an action is a slug, as the review queue reads it"
        assert item["fingerprint"] == review.fingerprint(
            item["rule"], next(v.key for v in found if v.rule == item["rule"])
        )
        assert item["detail"]["required"] == 1 and item["cluster"] == "c1"
    assert review_items(check(snapshot(list(reversed(nodes)), []), rules_of(ONTOLOGY))) == review_items(found)


def test_the_example_of_docs_kg_md_is_a_valid_ontology_with_rules():
    text = (Path(__file__).parents[2] / "docs" / "kg.md").read_text()
    example = yaml.safe_load(re.search(r"## Ontology rules.*?```yaml\n(.*?)```", text, re.S).group(1))
    assert validate_ontology(example) == []
    rules = rules_of(example)
    assert rules.relations["run_by_team"].cardinality == "many_to_one" and rules.relations["contains"].acyclic
    assert rules.kinds["Service"].keys == (("slug",), ("team", "port"))
