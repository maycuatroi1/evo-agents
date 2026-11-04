"""KG strategies: the built-in files, a project's own, what apply changes and what it never touches."""

import json

import pytest
import yaml

from evo_agents.cli import main
from evo_agents.kg import claude_code, entities, init_run, strategy
from evo_agents.kg import settings as kg_settings
from evo_agents.kg.build import build_project
from evo_agents.kg.project import load_project_at
from evo_agents.kg.schema import HUB_KINDS, HUB_RELATIONS
from evo_agents.kg.sync import sync_project
from tests.kg.projects import make_project, write
from tests.kg.test_entities_claude_code import fake_claude  # noqa: F401 (a fixture)
from tests.kg.test_extract_entities import entity_nodes

BUILT_IN = {"code", "documents", "records", "conversation", "media-metadata", "multi-hop"}


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


def test_multi_hop_holds_the_documents_keys_the_entities_step_on_claude_code_and_the_walk():
    found = strategy.builtins()
    hop, docs = found["multi-hop"], found["documents"]
    assert hop.suits == ("prose",) and hop.steps == ("entities",)
    assert {k: hop.settings[k] for k in docs.settings} == docs.settings, "every key of documents, same values"
    tuning = kg_settings.of(hop.settings)
    assert tuning.walk is not None and tuning.walk["context"] is True, "the walk is on with its defaults"
    mapping = tuning.entities
    assert mapping["model"] == "claude-haiku-5-5" and claude_code.uses(mapping["command"])
    assert mapping["sink"] == "llm", "Claude Code sends the text off the machine: the mapping names the sink"
    kinds, rels = hop.ontology["node_kinds"], hop.ontology["relations"]
    assert set(mapping["node_kinds"]) == set(kinds) and set(mapping["relations"]) == set(rels)
    assert not set(kinds) & set(HUB_KINDS) and not set(rels) & set(HUB_RELATIONS), "a shared ontology of its own"


def test_the_commands_list_show_and_apply_multi_hop_and_name_the_keys_to_build_again(tmp_path, kg_env, capsys):
    project = make_project(tmp_path, kg_env)
    root = str(project.harness.root)
    assert main(["kg", "strategy", "list", "--project", root]) == 0
    assert "multi-hop" in capsys.readouterr().out
    assert main(["kg", "strategy", "show", "multi-hop", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["steps"] == ["entities"] and shown["settings"]["entities"]["sink"] == "llm"
    assert main(["kg", "strategy", "apply", "multi-hop", "--project", root, "--dry-run", "--json"]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["applied"] is False and not (project.root / kg_settings.SETTINGS_FILE).exists()
    assert {"strategy", "entities", "passage_chars", "semantic_mode", "ontology"} <= set(planned["needs_rebuild"])
    assert "walk" not in planned["needs_rebuild"] and "walk" in planned["changed"], "the walk is a query key"
    assert planned["adds_ontology"]["node_kinds"] == ["Concept", "Actor", "Organization", "Place", "Event", "Work"]
    assert main(["kg", "strategy", "apply", "multi-hop", "--project", root, "--dry-run"]) == 0
    text = capsys.readouterr().out
    assert "would change passage_chars: 1500 -> 1800" in text and "takes effect with the next build: " in text
    assert main(["kg", "strategy", "apply", "multi-hop", "--project", root, "--json"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["needs_rebuild"] == planned["needs_rebuild"]
    project = load_project_at(project.harness.root, kg_env)
    tuning = kg_settings.read(project.root)
    assert tuning.strategy == "multi-hop" and tuning.walk is not None
    assert entities.enabled(tuning, project.harness.root) == tuning.entities, "the step runs under this strategy"
    assert set(project.ontology["node_kinds"]) >= {"Concept", "Actor", "Work"}


def test_multi_hop_sends_nothing_until_the_project_declares_the_llm_sink(tmp_path, kg_env, fake_claude):  # noqa: F811
    calls, _ = fake_claude
    project = make_project(tmp_path, kg_env)
    strategy.apply(strategy.builtins()["multi-hop"], project)
    kg_settings.write(project.root, kg_settings.of({"semantic_mode": "off"}, kg_settings.read(project.root)))
    project = load_project_at(project.harness.root, kg_env)
    assert all(r.ok for r in sync_project(project))
    report = build_project(project)
    assert report.ok, report.errors
    cov = report.coverage["entities"]
    assert calls() == [] and cov["calls"] == 0 and cov["refused_count"] >= 1
    assert "sink 'llm' is not declared" in cov["refused"][0]["reason"]
    data = yaml.safe_load(project.knowledge_path.read_text())
    data["policy"]["sinks"].append({"id": "llm", "kind": "llm-backend", "clearance": {"level": "internal"}})
    project.knowledge_path.write_text(yaml.safe_dump(data, sort_keys=False))
    project = load_project_at(project.harness.root, kg_env)
    report = build_project(project)
    assert report.ok, report.errors
    cov = report.coverage["entities"]
    assert cov["calls"] == len(calls()) >= 1 and cov["model"] == "claude-haiku-5-5" and cov["sink"] == "llm"
    assert {"entity:Concept:search", "entity:Concept:server"} <= {r[0] for r in entity_nodes(project)}


def test_kg_init_offers_multi_hop_for_a_project_made_of_prose():
    pool = strategy.builtins()
    prose = {"lines": 1000, "classes": {"prose": {"lines": 900}, "code": {"lines": 100}}}
    assert "multi-hop" in init_run.suits(prose, pool)
    assert "multi-hop" not in init_run.suits({"lines": 1000, "classes": {"code": {"lines": 1000}}}, pool)
    lines = init_run.prompt_lines(
        project="demo",
        why=[],
        profile_text="",
        coverage=None,
        strategies=[s.to_json() for s in pool.values()],
        measures=None,
        settings={},
    )
    assert any(line.startswith("- multi-hop (built-in)") for line in lines)
