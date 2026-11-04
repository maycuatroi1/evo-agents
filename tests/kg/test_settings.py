"""A project's KG settings (``evo_agents.kg.settings``): defaults that are today's constants, a file of overrides next
to semantic.json, a change history that undoes, query keys that act at once and build keys that wait for a build."""

import json

import pytest

from evo_agents.cli import main
from evo_agents.kg import search as text_search
from evo_agents.kg import semantic
from evo_agents.kg import settings as kg_settings
from evo_agents.kg.build import build_inputs
from evo_agents.kg.store import Store
from tests.kg.projects import commit, make_project, write
from tests.kg.test_store import make_searchable, sync_build


def test_a_project_that_set_nothing_is_tuned_by_the_constants_the_code_always_had(tmp_path):
    tuning = kg_settings.read(tmp_path)
    assert tuning == kg_settings.DEFAULT and tuning.overrides() == {}
    assert tuning.bm25 == text_search.WEIGHTS
    assert tuning.semantic_weight == semantic.SEMANTIC_WEIGHT
    assert (tuning.passage_from, tuning.passage_chars) == (text_search.PASSAGE_FROM, text_search.PASSAGE_CHARS)
    assert tuning.embed_chars == semantic.EMBED_CHARS and tuning.semantic_mode == "off"


def test_semantic_json_is_still_read_and_settings_json_holds_the_rest(tmp_path):
    semantic.write_settings(tmp_path, semantic.Settings("index", "some/model"))
    (tmp_path / kg_settings.SETTINGS_FILE).write_text(json.dumps({"bm25_body": 3, "passage_from": 6000}))
    tuning = kg_settings.read(tmp_path)
    assert (tuning.semantic_mode, tuning.semantic_model) == ("index", "some/model")
    assert tuning.bm25_body == 3.0 and tuning.passage_from == 6000 and tuning.bm25_name == text_search.WEIGHTS[2]


@pytest.mark.parametrize(
    "key, value",
    [
        ("nope", 1),
        ("semantic_mode", "maybe"),
        ("semantic_weight", -1),
        ("bm25_body", "heavy"),
        ("passage_from", 12.5),
        ("embed_chars", 10),
        ("strategy", "Not A Name"),
    ],
)
def test_a_value_out_of_range_is_refused_with_the_key_named(key, value):
    with pytest.raises(kg_settings.SettingsError, match=key):
        kg_settings.of({key: value})


def test_a_write_keeps_who_what_and_why_and_undo_puts_back_then_undoes_itself(tmp_path):
    first = kg_settings.write(
        tmp_path, kg_settings.of({"semantic_weight": 1.0}), reason="meaning helps", figures={"hit5": 0.61}
    )
    assert first.semantic_weight == 1.0 and kg_settings.read(tmp_path) == first
    kg_settings.write(tmp_path, kg_settings.of({"bm25_body": 2.0}, first), reason="body")
    found = kg_settings.history(tmp_path)
    assert [h["reason"] for h in found] == ["meaning helps", "body"] and found[0]["figures"] == {"hit5": 0.61}
    assert kg_settings.undo(tmp_path).bm25_body == text_search.WEIGHTS[4]  # the body weight is back
    assert kg_settings.read(tmp_path).semantic_weight == 1.0
    assert kg_settings.undo(tmp_path).bm25_body == 2.0  # a second undo undoes the undo
    kg_settings.write(tmp_path, kg_settings.DEFAULT)
    assert not (tmp_path / kg_settings.SETTINGS_FILE).exists()  # all defaults: no file


def test_a_key_needs_a_build_when_what_a_build_writes_changes():
    base = kg_settings.DEFAULT
    assert kg_settings.rebuild_keys(base, kg_settings.of({"semantic_weight": 2, "bm25_body": 2})) == []
    assert kg_settings.rebuild_keys(base, kg_settings.of({"passage_from": 5000, "embed_chars": 1000})) == [
        "passage_from",
        "embed_chars",
    ]
    on = kg_settings.of({"semantic_mode": "index"})
    assert kg_settings.rebuild_keys(base, on) == ["semantic_mode"]  # vectors come with a build
    assert kg_settings.rebuild_keys(on, kg_settings.of({"semantic_mode": "on"}, on)) == []  # on is read at once


def test_embed_chars_cuts_the_text_of_a_row():
    assert len(semantic.embed_text("name", "path", "x" * 5000)) == semantic.EMBED_CHARS
    assert len(semantic.embed_text("name", "path", "x" * 5000, 300)) == 300


def test_the_build_inputs_change_only_for_a_project_that_set_a_build_key(tmp_path, kg_env):
    project = make_searchable(tmp_path, kg_env)
    corpus = project.corpus()
    try:
        plain = build_inputs(project, corpus)
        kg_settings.write(project.root, kg_settings.of({"bm25_body": 2.0}))
        assert build_inputs(project, corpus) == plain  # a query key leaves the graph alone
        kg_settings.write(project.root, kg_settings.of({"passage_from": 1000}))
        assert build_inputs(project, corpus) != plain
    finally:
        corpus.close()


def long_page(words: int) -> str:
    return "\n\n".join(f"Paragraph {i} about the garage and the cars parked there at night." for i in range(words))


def test_passage_from_cuts_a_shorter_body_into_passages_and_a_build_applies_it(tmp_path, kg_env):
    project = make_searchable(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    write(app / "docs/long.md", "# Long\n\n" + long_page(40))
    commit(app, "long page")
    sync_build(project)
    store = Store.for_project(project)
    default_rows = store.db.execute("SELECT count(*) FROM node_fts").fetchone()[0]
    store.close()
    kg_settings.write(project.root, kg_settings.of({"passage_from": 800, "passage_chars": 300}))
    sync_build(project)
    store = Store.for_project(project)
    tuned_rows = store.db.execute("SELECT count(*) FROM node_fts").fetchone()[0]
    store.close()
    assert tuned_rows > default_rows


def test_a_query_key_acts_on_the_next_search_without_a_build(tmp_path, kg_env):
    project = make_searchable(tmp_path, kg_env)
    sync_build(project)
    store = Store.open_readonly(project)
    assert store.tuning == kg_settings.DEFAULT
    store.close()
    kg_settings.write(project.root, kg_settings.of({"bm25_name": 9.0, "semantic_weight": 2.0}))
    store = Store.open_readonly(project)
    try:
        assert store.tuning.bm25[2] == 9.0 and store.tuning.semantic_weight == 2.0
    finally:
        store.close()
    with kg_settings.overriding({"bm25_name": 1.5}):  # a run asking for a candidate wins over the file
        store = Store.open_readonly(project)
        try:
            assert store.tuning.bm25_name == 1.5 and store.tuning.semantic_weight == semantic.SEMANTIC_WEIGHT
        finally:
            store.close()
    assert kg_settings.override_of() is None


def test_the_settings_command_shows_sets_with_a_reason_and_undoes(tmp_path, kg_env, capsys):
    project = make_project(tmp_path, kg_env)
    argv = ["kg", "settings", "--project", str(project.harness.root), "--json"]
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out)["changed"] == {}
    assert main([*argv, "--set", "semantic_weight=1.5", "--set", "passage_from=6000", "--reason", "trial"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["settings"]["semantic_weight"] == 1.5 and out["needs_rebuild"] == ["passage_from"]
    assert out["changed"]["semantic_weight"] == {"from": semantic.SEMANTIC_WEIGHT, "to": 1.5}
    assert main([*argv, "--set", "semantic_weight=-3"]) == 2
    assert "semantic_weight" in capsys.readouterr().err
    assert main([*argv, "--undo"]) == 0
    assert json.loads(capsys.readouterr().out)["settings"]["semantic_weight"] == semantic.SEMANTIC_WEIGHT
