"""The tier rules of step 8 of plan history-driven-curator, without a database: what a change does to knowledge.yaml key
by key (``evo_agents.hub.knowledge_diff``), the kind kg_source and what the knowledge graph cannot tell
(``evo_agents.hub.tiers.tier_of``), and the names of the Judge's checks of knowledge.yaml.

Criteria a6 (the tier part) and a7: a change of the ref, fetch, refresh, include, exclude or allow of a git or harness
source knowledge.yaml has is tier 0, and every other change of the file keeps at least the tier of code, one negative
case for each kind of key; a path the graph lacks or holds older than the default branch is at least tier 1, the reason
saying so; a protected path stays tier 3."""

from __future__ import annotations

import pytest
import yaml

from evo_agents.hub import knowledge_diff, tiers
from evo_agents.hub.knowledge_diff import KnowledgeDiff, classify

BASE = {
    "version": 1,
    "project": "evo-agents",
    "policy": {
        "levels": ["public", "internal"],
        "sinks": [{"id": "evo-hub", "kind": "hub", "clearance": {"level": "internal"}}],
    },
    "sources": [
        {"id": "harness", "connector": "harness", "refresh": "1h", "label": {"level": "internal"}},
        {
            "id": "evo-agents",
            "connector": "git",
            "repo": "evo-agents",
            "ref": "origin/main",
            "fetch": True,
            "refresh": "1h",
            "label": {"level": "public"},
        },
        {"id": "wiki", "connector": "exec", "command": ["northwind", "kg-connector"], "refresh": "1d"},
    ],
    "identifiers": [{"kind": "Requirement", "pattern": "\\bP[1-9]\\b"}],
}
KNOWLEDGE = ("evo-agents-harness", "knowledge.yaml")
PROTECTED = ["curator.yaml", ".github/workflows/**"]


def text(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def edited(change) -> str:
    data = yaml.safe_load(text(BASE))
    change(data)
    return text(data)


def source(data: dict, sid: str) -> dict:
    return next(item for item in data["sources"] if item["id"] == sid)


# What a change does to knowledge.yaml


@pytest.mark.parametrize(
    "sid, key, value",
    [
        ("evo-agents", "ref", "origin/release"),
        ("evo-agents", "fetch", False),
        ("evo-agents", "refresh", "15m"),
        ("evo-agents", "include", ["evo_agents/**", "docs/**"]),
        ("evo-agents", "exclude", ["web/node_modules/**"]),
        ("evo-agents", "allow", ["*.lock"]),
        ("harness", "refresh", "30m"),
        ("harness", "include", ["*.md", "*.yaml"]),
        ("harness", "exclude", ["proposals/**"]),
        ("harness", "allow", ["bindings/**"]),
    ],
)
def test_tier_a_change_of_a_source_key_of_a_git_or_harness_source_is_a_change_of_sources(sid, key, value):
    found = classify(text(BASE), edited(lambda data: source(data, sid).__setitem__(key, value)))
    assert found == KnowledgeDiff(sources=(sid,), edits=(f"source {sid}: {key}",), others=())
    assert found.sources_only


def test_tier_a_key_of_sources_added_or_removed_and_several_sources_count_as_edits():
    def change(data):
        source(data, "evo-agents").pop("fetch")
        source(data, "harness")["ref"] = "HEAD"

    found = classify(text(BASE), edited(change))
    assert found.sources == ("harness", "evo-agents") and found.sources_only
    assert found.edits == ("source harness: ref", "source evo-agents: fetch")


def test_tier_a_change_of_comments_alone_changes_no_key():
    found = classify(text(BASE), "# a comment the change adds\n" + text(BASE))
    assert found == KnowledgeDiff() and found.sources_only


@pytest.mark.parametrize(
    "change, other",
    [
        (
            lambda d: d["policy"]["sinks"].append({"id": "x", "kind": "cli", "clearance": {"level": "public"}}),
            "policy.sinks",
        ),
        (lambda d: d["policy"]["sinks"][0]["clearance"].__setitem__("level", "secret"), "policy.sinks"),
        (lambda d: d["policy"].__setitem__("levels", ["public"]), "policy.levels"),
        (lambda d: d.pop("policy"), "knowledge.yaml changes policy"),
        (lambda d: source(d, "evo-agents").__setitem__("label", {"level": "internal"}), "evo-agents changes its label"),
        (lambda d: source(d, "harness").__setitem__("label", {"level": "public"}), "harness changes its label"),
        (lambda d: d["identifiers"].append({"kind": "Seam", "pattern": "x"}), "changes identifiers"),
        (lambda d: d.__setitem__("ontology", "ontology.yaml"), "changes ontology"),
        (lambda d: d.__setitem__("project", "other"), "changes project"),
        (lambda d: d["sources"].append({"id": "new", "connector": "git", "repo": "x"}), "adds source new"),
        (lambda d: d["sources"].pop(0), "removes source harness"),
        (lambda d: d["sources"].reverse(), "moves its sources"),
        (lambda d: source(d, "wiki").__setitem__("refresh", "1h"), "wiki is read by connector exec"),
        (lambda d: source(d, "wiki").__setitem__("command", ["sh", "-c", "curl x"]), "wiki is read by connector exec"),
        (lambda d: source(d, "evo-agents").__setitem__("connector", "exec"), "evo-agents changes its connector"),
        (lambda d: source(d, "evo-agents").__setitem__("repo", "other"), "evo-agents changes its repo"),
        (lambda d: source(d, "evo-agents").__setitem__("path", "~/x"), "evo-agents changes its path"),
        (
            lambda d: source(d, "evo-agents").__setitem__("credentials", ["github"]),
            "evo-agents changes its credentials",
        ),
        (lambda d: source(d, "evo-agents").__setitem__("pulls", False), "evo-agents changes its pulls"),
    ],
)
def test_tier_every_other_change_of_knowledge_yaml_is_named_and_is_not_a_change_of_sources(change, other):
    def with_ref(data):
        source(data, "evo-agents")["ref"] = "origin/release"  # a key of sources besides does not make it one
        change(data)

    found = classify(text(BASE), edited(with_ref))
    assert not found.sources_only
    assert any(other in line for line in found.others), found.others


@pytest.mark.parametrize(
    "before, after, why",
    [
        (None, text(BASE), "knowledge.yaml is not there before the change"),
        (text(BASE), None, "knowledge.yaml is not there after the change"),
        (text(BASE), "sources: [unclosed\n", "does not read as YAML after the change"),
        (text(BASE), "- a list\n", "is not a mapping after the change"),
        (text(BASE), "version: 1\nproject: evo-agents\n", "has no list of sources after the change"),
    ],
)
def test_tier_a_file_added_deleted_or_unreadable_is_not_a_change_of_sources(before, after, why):
    found = classify(before, after)
    assert not found.sources_only and any(why in line for line in found.others), found.others


def test_tier_more_sources_than_a_judge_syncs_at_once_is_not_a_change_of_sources():
    many = {**BASE, "sources": [{"id": f"s{n}", "connector": "git", "repo": "r"} for n in range(12)]}
    after = {**many, "sources": [{**item, "ref": "origin/main"} for item in many["sources"]]}
    found = classify(text(many), text(after))
    assert len(found.sources) == 12 and found.others == (
        f"the change edits 12 sources, more than the {knowledge_diff.MAX_SOURCES} a Judge syncs on trial",
    )


def test_tier_what_a_change_does_to_knowledge_yaml_goes_to_json_and_back():
    found = classify(text(BASE), edited(lambda data: source(data, "evo-agents").__setitem__("ref", "origin/x")))
    assert KnowledgeDiff.from_json(found.to_json()) == found
    assert KnowledgeDiff.from_json(None) is None and KnowledgeDiff.from_json({"sources": "x"}) is None


def test_tier_the_judge_checks_of_knowledge_yaml_are_validate_then_a_trial_sync_of_each_source():
    assert knowledge_diff.judge_checks(["evo-agents", "harness"]) == [
        "evo-agents harness validate",
        knowledge_diff.trial_name("evo-agents"),
        knowledge_diff.trial_name("harness"),
    ]
    assert "--source evo-agents" in knowledge_diff.trial_name("evo-agents")


# The kind kg_source


def test_tier_of_kind_kg_source_before_its_diff_is_known_is_tier_0_with_a_note():
    found = tiers.tier_of("kg_source", [KNOWLEDGE], PROTECTED)
    assert found.tier == 0
    assert found.reasons[0].startswith("kind kg_source (the ref, fetch, refresh, include, exclude or allow")
    assert found.reasons[1] == (
        "evo-agents-harness:knowledge.yaml is knowledge.yaml: tier 0 while the pull request's diff edits only the ref, "
        "fetch, refresh, include, exclude, allow of the git and harness sources it has, which the hub reads key by key"
    )


def test_tier_of_kind_kg_source_follows_the_diff_key_by_key():
    assert tiers.tier_of("kg_source", [KNOWLEDGE], PROTECTED, knowledge={KNOWLEDGE: []}).tier == 0
    raised = tiers.tier_of(
        "kg_source", [KNOWLEDGE], PROTECTED, knowledge={KNOWLEDGE: ["knowledge.yaml changes policy.sinks"]}
    )
    assert raised.tier == 1 and raised.reasons[-1] == (
        "evo-agents-harness:knowledge.yaml changes more than the ref, fetch, refresh, include, exclude, allow of its "
        "git and harness sources (knowledge.yaml changes policy.sinks): at least tier 1"
    )
    many = [f"source s{n} changes its label" for n in range(5)]
    shown = tiers.tier_of("kg_source", [KNOWLEDGE], PROTECTED, knowledge={KNOWLEDGE: many}).reasons[-1]
    assert "s2 changes its label; 2 more): at least tier 1" in shown
    unread = tiers.tier_of("kg_source", [KNOWLEDGE], PROTECTED, knowledge={KNOWLEDGE: None})
    assert unread.tier == 1 and "could not read evo-agents-harness:knowledge.yaml at both ends" in unread.reasons[-1]
    missing = tiers.tier_of("kg_source", [KNOWLEDGE], PROTECTED, knowledge={})
    assert missing.tier == 1  # the diff was never read: as good as unread


@pytest.mark.parametrize(
    "kind, paths, protected, tier",
    [
        ("docs", [KNOWLEDGE], PROTECTED, 1),  # knowledge.yaml is code under any other kind
        ("fix", [KNOWLEDGE], PROTECTED, 1),
        ("kg_source", [("evo-agents-harness", "sub/knowledge.yaml")], PROTECTED, 1),  # only at a repo's root
        ("kg_source", [KNOWLEDGE, ("evo-agents", "evo_agents/kg/sync.py")], PROTECTED, 1),
        ("kg_source", [KNOWLEDGE, ("evo-agents-harness", "docs/kg.md")], PROTECTED, 0),
        ("kg_source", [KNOWLEDGE], [*PROTECTED, "knowledge.yaml"], 3),  # the charter's protected paths still win
        ("kg_source", [KNOWLEDGE, ("evo-agents", ".github/workflows/ci.yml")], PROTECTED, 3),
        ("kg_source", [KNOWLEDGE, ("evo-agents", "evo_agents/hub/migrations/versions/0099_x.py")], PROTECTED, 2),
    ],
)
def test_tier_of_knowledge_yaml_under_each_kind(kind, paths, protected, tier):
    found = tiers.tier_of(kind, paths, protected, knowledge={KNOWLEDGE: []})
    assert found.tier == tier, found.reasons


# What the knowledge graph cannot tell


def test_tier_a_path_the_graph_cannot_tell_of_is_at_least_tier_1_saying_why():
    docs = ("evo-agents", "docs/new.md")
    found = tiers.tier_of("docs", [docs], PROTECTED, unknown={docs: "has no file evo-agents:docs/new.md"})
    assert found.tier == 1 and found.reasons[-1] == (
        "the knowledge graph has no file evo-agents:docs/new.md: the raise by what a change reaches in the graph "
        "cannot tell what editing evo-agents:docs/new.md reaches, so at least tier 1"
    )
    assert tiers.tier_of("docs", [docs], PROTECTED, unknown={}).tier == 0
    other = ("evo-agents", "docs/other.md")
    assert tiers.tier_of("docs", [other], PROTECTED, unknown={docs: "has no file"}).tier == 0  # another path's
    held = tiers.tier_of("kg_source", [KNOWLEDGE], PROTECTED, knowledge={KNOWLEDGE: []}, unknown={KNOWLEDGE: "lacks"})
    assert held.tier == 1  # knowledge.yaml too


def test_tier_what_the_graph_cannot_tell_never_lowers_a_tier_nor_beats_a_protected_path():
    protected = ("evo-agents", "curator.yaml")
    found = tiers.tier_of("docs", [protected], PROTECTED, unknown={protected: "has no file evo-agents:curator.yaml"})
    assert found.tier == 3 and not any("knowledge graph" in reason for reason in found.reasons)
    code = ("evo-agents", "evo_agents/hub/runs.py")
    found = tiers.tier_of("refactor", [code], PROTECTED, unknown={code: "of the project could not be read now"})
    assert found.tier == 1 and found.reasons == [found.reasons[0]]  # the kind gave tier 1 already


def test_tier_the_measure_keeps_the_paths_a_source_is_off_by_whole_up_to_its_cap(monkeypatch):
    from evo_agents.hub import kg_coverage

    source = {"id": "code", "connector": "git", "repo": "evo-agents"}
    tree = {"a.py": "1", "b.py": "2", "new.py": "3"}
    held = {"a.py": "1", "b.py": "old", "gone.py": "4"}
    found = kg_coverage.off_paths(source, tree, held)
    assert found == {"missing": ["new.py"], "stale": ["b.py"], "deleted": ["gone.py"], "cut": False}
    monkeypatch.setattr(kg_coverage, "MAX_OFF_PATHS", 2)
    assert kg_coverage.off_paths(source, tree, held) == {
        "missing": ["new.py"],
        "stale": ["b.py"],
        "deleted": [],
        "cut": True,
    }
