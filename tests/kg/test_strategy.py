"""KG strategies: the built-in files, a project's own, what apply changes and what it never touches."""

import json

import pytest
import yaml

from evo_agents.cli import main
from evo_agents.kg import settings as kg_settings
from evo_agents.kg import strategy
from evo_agents.kg.build import build_project
from evo_agents.kg.project import load_project_at
from evo_agents.kg.sync import sync_project
from tests.kg.projects import make_project, write

BUILT_IN = {"code", "documents", "records", "conversation", "media-metadata"}


def own(root, name="mine", **extra):
    data = {"version": 1, "name": name, "settings": {"passage_chars": 900}} | extra
    path = root / "strategies" / f"{name}.yaml"
    write(path, yaml.safe_dump(data))
    return path


def test_every_built_in_strategy_is_valid_and_the_five_kinds_exist():
    found = strategy.builtins()
    assert BUILT_IN <= set(found)
    assert all(s.origin == "built-in" for s in found.values())
    assert found["code"].settings == {} and found["code"].steps == ()


def test_a_file_with_a_mistake_names_every_problem():
    bad = {
        "version": 2,
        "name": "Bad Name",
        "settings": {"strategy": "x", "passage_chars": 5},
        "extra": 1,
        "steps": ["nope"],
        "knowledge": {"sources": []},
        "suits": ["music"],
    }
    with pytest.raises(strategy.StrategyError) as e:
        strategy.parse(bad)
    text = str(e.value)
    for part in (
        "unknown key 'extra'",
        "version must be 1",
        "name must be",
        "not a class",
        "does not touch sources",
        "set by apply",
        "not a build step",
    ):
        assert part in text


def test_an_identifier_that_is_not_a_regular_expression_is_refused():
    with pytest.raises(strategy.StrategyError, match="regular expression"):
        strategy.parse({"version": 1, "name": "x", "knowledge": {"identifiers": [{"kind": "Ticket", "pattern": "("}]}})


def test_a_project_strategy_wins_over_a_built_in_of_the_same_name(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    own(project.harness.root, "documents")
    found = strategy.available(project.harness.root)
    assert found["documents"].origin.endswith("documents.yaml") and found["documents"].settings == {
        "passage_chars": 900
    }
    assert strategy.find("records", project.harness.root).origin == "built-in"
    with pytest.raises(strategy.StrategyError, match="no strategy named"):
        strategy.find("nothing", project.harness.root)


def test_apply_sets_the_settings_and_says_which_need_a_build(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    done = strategy.apply(strategy.builtins()["documents"], project)
    now = kg_settings.read(project.root)
    assert (now.strategy, now.passage_chars, now.semantic_mode) == ("documents", 1800, "index")
    assert {"strategy", "passage_chars"} <= set(done.needs_rebuild)
    assert kg_settings.history(project.root)[-1]["reason"] == "strategy documents"


def test_apply_never_changes_a_source_or_its_label(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    sources = [dict(s) for s in project.sources()]
    strategy.apply(strategy.builtins()["records"], project)
    after = load_project_at(project.harness.root, kg_env)
    assert after.sources() == sources
    assert [i["kind"] for i in after.knowledge["identifiers"]][-1] == "Ticket"


def test_apply_adds_the_identifiers_and_ontology_it_lacks_and_keeps_comments(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    path = project.knowledge_path
    data = yaml.safe_load(path.read_text())
    data.pop("identifiers", None)
    path.write_text("# kept by the strategy\n" + yaml.safe_dump(data, sort_keys=False))
    project = load_project_at(project.harness.root, kg_env)
    before = path.read_text()
    assert "identifiers" not in project.knowledge
    chosen = own(
        project.harness.root,
        "screens",
        knowledge={
            "identifiers": [{"kind": "Screen", "pattern": r"\bSCR-\d+\b"}],
            "ontology": {"node_kinds": ["Screen"]},
        },
    )
    done = strategy.apply(strategy.load_file(chosen), project)
    text = path.read_text()
    assert text.startswith(before.rstrip("\n")), "the file's own text is kept when the keys are new"
    after = load_project_at(project.harness.root, kg_env)
    assert after.ontology == {"node_kinds": ["Screen"]}
    assert (path.with_name("knowledge.yaml.before-strategy")).read_text() == before
    again = strategy.plan(strategy.load_file(chosen), after)
    assert again.identifiers == [] and again.ontology == {} and done.identifiers
    report = build_project(after)  # the project still builds with what the strategy wrote
    assert report.ok, report.errors


def test_apply_to_a_project_that_already_has_the_kind_adds_nothing_twice(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    strategy.apply(strategy.builtins()["records"], project)
    project = load_project_at(project.harness.root, kg_env)
    again = strategy.plan(strategy.builtins()["records"], project)
    assert again.identifiers == [] and again.changed == {}


def test_a_strategy_that_needs_a_connector_the_project_lacks_changes_nothing(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    chosen = strategy.load_file(own(project.harness.root, "crm", requires={"connectors": ["crm-export"]}))
    before = project.knowledge_path.read_text()
    with pytest.raises(strategy.StrategyError, match="crm-export"):
        strategy.apply(chosen, project)
    assert project.knowledge_path.read_text() == before and not (project.root / kg_settings.SETTINGS_FILE).exists()


def test_the_code_strategy_builds_the_graph_a_project_without_one_gets(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    assert all(r.ok for r in sync_project(project))
    plain = build_project(project)
    strategy.apply(strategy.builtins()["code"], project)
    coded = build_project(project)
    assert coded.ok and coded.content_hash == plain.content_hash
    assert coded.nodes == plain.nodes and coded.edges == plain.edges


def test_the_command_lists_shows_and_applies_with_a_dry_run(tmp_path, kg_env, capsys):
    project = make_project(tmp_path, kg_env)
    root = str(project.harness.root)
    assert main(["kg", "strategy", "list", "--project", root, "--json"]) == 0
    assert BUILT_IN <= {s["name"] for s in json.loads(capsys.readouterr().out)}
    assert main(["kg", "strategy", "apply", "documents", "--project", root, "--dry-run", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is False and out["settings"]["strategy"] == "documents"
    assert not (project.root / kg_settings.SETTINGS_FILE).exists()
    assert main(["kg", "strategy", "apply", "documents", "--project", root, "--json"]) == 0
    assert kg_settings.read(project.root).strategy == "documents"
    assert main(["kg", "strategy", "apply", "nothing", "--project", root]) == 2
