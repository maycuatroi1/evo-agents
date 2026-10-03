"""The ontology knowledge.yaml points at decides which node kinds a build accepts."""

import pytest

from evo_agents.kg.build import build_project
from evo_agents.kg.project import ProjectError, load_project_at
from evo_agents.kg.store import Store
from evo_agents.kg.sync import sync_project
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


def test_ontology_kinds_build(tmp_path, kg_env):
    project = with_screens(make_project(tmp_path, kg_env), "node_kinds: [Screen]\n")
    assert project.ontology == {"node_kinds": ["Screen"]}
    report = build_project(project)
    assert report.ok, report.errors
    assert Store.for_project(project).node("screen:SCR-7", report.build_id)["kind"] == "Screen"


def test_kind_outside_the_ontology_fails_the_build_naming_the_item(tmp_path, kg_env):
    project = with_screens(make_project(tmp_path, kg_env), "node_kinds: [Widget]\n")
    report = build_project(project)
    assert not report.ok
    assert report.errors == [f"{NOTES}: node screen:SCR-7 has kind 'Screen', neither a hub kind nor in the ontology"]


def test_missing_ontology_fails_the_build(tmp_path, kg_env):
    project = with_screens(make_project(tmp_path, kg_env), "node_kinds: [Screen]\n")
    (project.harness.root / "ontology.yaml").unlink()
    with pytest.raises(ProjectError, match="file not found"):
        _ = project.ontology
    report = build_project(project)
    assert not report.ok
    assert len(report.errors) == 1 and "ontology.yaml: file not found" in report.errors[0]


def test_invalid_ontology_fails_the_build(tmp_path, kg_env):
    project = with_screens(make_project(tmp_path, kg_env), "node_kinds: [Screen, Document]\n")
    report = build_project(project)
    assert not report.ok
    assert "'Document' is a hub kind" in report.errors[0]


def test_without_ontology_key_any_kind_builds_as_before(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    write(project.harness.root / "ontology.yaml", "node_kinds:\n  parsed_from_srs: [Entity]\n")
    project = with_screens(project, None)
    assert project.ontology is None
    report = build_project(project)
    assert report.ok, report.errors
    assert Store.for_project(project).node("screen:SCR-7", report.build_id)["kind"] == "Screen"
