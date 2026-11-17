"""The ontology knowledge.yaml points at decides which node kinds a build accepts."""

import pytest

from evo_agents.kg.project import ProjectError, load_project_at
from evo_agents.kg.sync import sync_project
from tests.kg import built
from tests.kg.projects import make_project, write

SCREEN = "  - kind: Screen\n    pattern: '\\bSCR-\\d+\\b'\n"
NOTES = "harness:file:notes.md"


def with_screens(project, ontology: str | None):
    """Add a Screen identifier, a note that mentions one, and optionally an ontology; reload."""
    root = project.harness.root
    text = (root / "knowledge.yaml").read_text() + SCREEN
    if ontology is not None:
        text += "ontology: ontology.yaml\n"
        write(root / "ontology.yaml", ontology)
    write(root / "knowledge.yaml", text)
    write(root / "notes.md", "# Notes\n\nThe results screen SCR-7 lists what KB-01 returns.\n")
    project = load_project_at(root, project.home)
    results = sync_project(project)
    assert all(r.ok for r in results), [r.issues for r in results]
    return project


def kind_of(project, node_id: str) -> str:
    return next(n["kind"] for n in built.nodes_of(project) if n["id"] == node_id)


def test_ontology_kinds_build(tmp_path, kg_env):
    project = with_screens(make_project(tmp_path, kg_env), "node_kinds: [Screen]\n")
    assert project.ontology == {"node_kinds": ["Screen"]}
    report = built.build(project)
    assert report.ok, report.errors
    assert kind_of(project, "screen:SCR-7") == "Screen"


def test_kind_outside_the_ontology_fails_the_build_naming_the_item(tmp_path, kg_env):
    project = with_screens(make_project(tmp_path, kg_env), "node_kinds: [Widget]\n")
    report = built.build(project)
    assert not report.ok
    assert report.errors == [f"{NOTES}: node screen:SCR-7 has kind 'Screen', neither a hub kind nor in the ontology"]


def test_missing_ontology_fails_the_build(tmp_path, kg_env):
    project = with_screens(make_project(tmp_path, kg_env), "node_kinds: [Screen]\n")
    (project.harness.root / "ontology.yaml").unlink()
    with pytest.raises(ProjectError, match="file not found"):
        _ = project.ontology
    report = built.build(project)
    assert not report.ok
    assert len(report.errors) == 1 and "ontology.yaml: file not found" in report.errors[0]


def test_invalid_ontology_fails_the_build(tmp_path, kg_env):
    project = with_screens(make_project(tmp_path, kg_env), "node_kinds: [Screen, Document]\n")
    report = built.build(project)
    assert not report.ok
    assert "'Document' is a hub kind" in report.errors[0]


def test_without_ontology_key_any_kind_builds_as_before(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    write(project.harness.root / "ontology.yaml", "node_kinds:\n  parsed_from_srs: [Entity]\n")
    project = with_screens(project, None)
    assert project.ontology is None
    report = built.build(project)
    assert report.ok, report.errors
    assert kind_of(project, "screen:SCR-7") == "Screen"


def test_commits_and_pull_requests_are_hub_kinds_an_ontology_may_not_redefine(tmp_path, kg_env):
    from evo_agents.kg.schema import HUB_KINDS, HUB_RELATIONS, validate_ontology

    assert {"Commit", "PullRequest"} <= set(HUB_KINDS)
    assert HUB_RELATIONS["changes"] == ("Commit", "*") and HUB_RELATIONS["merged_as"] == ("PullRequest", "Commit")
    issues = validate_ontology({"node_kinds": ["PullRequest"], "relations": ["changes"]})
    assert [i.message for i in issues] == [
        "'PullRequest' is a hub kind; extend it with properties instead",
        "'changes' is a hub relation",
    ]
    project = with_screens(make_project(tmp_path, kg_env), "node_kinds: [Screen]\n")
    report = built.build(project)
    assert report.ok, report.errors  # the commit items of the app's history build under an ontology
    commits = [n for n in built.nodes_of(project) if n["kind"] == "Commit" and n["id"].startswith("app:commit:")]
    assert len(commits) == 1


def test_a_build_puts_what_breaks_its_ontology_rules_in_the_review_queue(tmp_path, kg_env):
    ontology = "node_kinds:\n  - name: Screen\n    required_props: [owner]\n"
    project = with_screens(make_project(tmp_path, kg_env), ontology)
    report = built.build(project)
    assert report.ok, report.errors
    found = [item for item in built.rows_of(project).review if item["rule"] == "missing_prop"]
    assert [(item["evidence"], item["action"]) for item in found] == [(["screen:SCR-7"], "set_prop")]
    assert found[0]["detail"]["missing"] == ["owner"]
    assert report.coverage["review"]["missing_prop"] == 1
