"""The KG settings of a hub project: what a read gives, what a change and an undo store, what a build and a query
get. Postgres is not needed: the connection is a fake that answers the few statements these functions make."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")

from evo_agents.hub import kg_build, kg_graph, kg_settings
from evo_agents.kg import semantic
from evo_agents.kg import settings as kg


def run(coro):
    return asyncio.run(coro)


class Result:
    def __init__(self, value=None, rows=()):
        self.value, self.rows = value, list(rows)

    def scalar(self):
        return self.value

    def scalar_one(self):
        return self.value

    def __iter__(self):
        return iter(self.rows)


def stub(monkeypatch, overrides=None, mode="index", model=semantic.DEFAULT_MODEL, held=None):
    """A store whose kg_settings row is ``overrides``, whose kg_semantic row is mode and model, whose changes are ``held``."""
    state = SimpleNamespace(
        overrides=dict(overrides or {}), mode=mode, model=model, changes=list(held or []), writes=[]
    )

    class Conn:
        async def execute(self, statement):
            kind = statement.__visit_name__
            table = statement.table.name if kind == "insert" else None
            if kind == "select" and statement.get_final_froms()[0].name == "kg_settings":
                return Result(state.overrides)
            if table == "kg_settings_changes":
                params = statement.compile().params
                state.changes.insert(
                    0,
                    kg_settings.Change(
                        len(state.changes) + 1,
                        params["kind"],
                        params["changed_by"],
                        params["run_id"],
                        params["reason"],
                        params["figures"],
                        params["before"],
                        params["after"],
                        params["undoes"],
                        None,
                    ),
                )
                return Result(len(state.changes))
            if table == "kg_settings":
                state.overrides = dict(statement.compile().params["overrides"])
                return Result()
            raise AssertionError(str(statement))

    async def read_semantic(conn, project_id):
        return semantic.Settings(state.mode, state.model)

    async def write_semantic(conn, project_id, settings, user_id=None):
        state.mode, state.model = settings.mode, settings.model

    async def changes(conn, project_id, limit=20):
        return state.changes[:limit]

    monkeypatch.setattr(kg_settings.kg_semantic, "read", read_semantic)
    monkeypatch.setattr(kg_settings.kg_semantic, "write", write_semantic)
    monkeypatch.setattr(kg_settings, "changes", changes)
    return state, Conn()


def test_a_project_without_a_row_is_tuned_by_the_defaults_and_its_semantic_row(monkeypatch):
    state, conn = stub(monkeypatch, mode="off")
    assert run(kg_settings.read(conn, 1)) == kg.of({"semantic_mode": "off"})
    state.overrides = {"semantic_weight": 1.5}
    tuned = run(kg_settings.read(conn, 1))
    assert tuned.semantic_weight == 1.5 and tuned.semantic_mode == "off"


def test_a_row_with_a_key_of_a_later_version_keeps_the_known_keys(monkeypatch):
    _, conn = stub(monkeypatch, overrides={"semantic_weight": 2.0, "from_the_future": 1})
    assert run(kg_settings.read(conn, 1)).semantic_weight == 2.0


def test_a_change_keeps_the_override_the_semantic_row_and_who_why_and_the_figures(monkeypatch):
    state, conn = stub(monkeypatch)
    before, after, made = run(
        kg_settings.change(
            conn,
            1,
            {"semantic_weight": 1.0, "semantic_mode": "on"},
            user_id=7,
            run_id=9,
            reason="holdout",
            figures={"mrr": 0.4},
        )
    )
    assert state.overrides == {"semantic_weight": 1.0} and state.mode == "on"
    first = state.changes[0]
    assert (first.changed_by, first.run_id, first.reason, first.figures, first.kind) == (
        7,
        9,
        "holdout",
        {"mrr": 0.4},
        "set",
    )
    assert first.before["semantic_weight"] == kg.DEFAULT.semantic_weight and first.after["semantic_weight"] == 1.0
    assert made == 1 and before.semantic_mode == "index" and after.semantic_mode == "on"


def test_an_unknown_key_or_a_value_out_of_range_writes_nothing(monkeypatch):
    state, conn = stub(monkeypatch)
    for updates in ({"nope": 1}, {"semantic_weight": 1000}):
        with pytest.raises(kg.SettingsError):
            run(kg_settings.change(conn, 1, updates, user_id=7))
    assert state.changes == [] and state.overrides == {}


def test_an_undo_puts_back_the_newest_change_not_undone_and_is_a_change_of_its_own(monkeypatch):
    state, conn = stub(monkeypatch)
    run(kg_settings.change(conn, 1, {"semantic_weight": 1.0}, user_id=7))
    run(kg_settings.change(conn, 1, {"semantic_weight": 2.0}, user_id=7))
    _, after, _ = run(kg_settings.undo(conn, 1, user_id=8))
    assert after.semantic_weight == 1.0 and state.overrides == {"semantic_weight": 1.0}
    assert (state.changes[0].kind, state.changes[0].undoes) == ("undo", 2)
    _, after, _ = run(kg_settings.undo(conn, 1, user_id=8))  # the one that is left, not the undone one again
    assert after.semantic_weight == kg.DEFAULT.semantic_weight and state.overrides == {}
    with pytest.raises(kg_settings.NothingToUndo):
        run(kg_settings.undo(conn, 1, user_id=8))


def test_a_build_reads_the_overrides_from_a_settings_file_and_none_leaves_no_file(tmp_path):
    kg_build.put_tuning(tmp_path, kg.of({"passage_from": 3000, "semantic_mode": "on"}))
    assert json.loads((tmp_path / kg.SETTINGS_FILE).read_text()) == {"passage_from": 3000}  # mode is in kg_semantic
    kg_build.put_tuning(tmp_path, kg.DEFAULT)
    assert not (tmp_path / kg.SETTINGS_FILE).exists()


def test_a_query_is_answered_with_the_tuning_of_the_project(monkeypatch):
    seen = {}

    class Opened:
        tuning = kg.DEFAULT

        def close(self):
            pass

    class Cache:
        def acquire(self, store, project, graphs):
            return "path", "graph"

        def release(self, path):
            pass

    class Handles:
        def call(self, session, owner, tool, arguments):
            seen["tuning"] = session.opened.tuning
            return {}

    monkeypatch.setattr(kg_graph, "open_graph", lambda *a: Opened())
    monkeypatch.setattr(kg_graph, "HubSession", lambda *a, **k: SimpleNamespace(opened=a[4], explorer=None))
    tuned = kg.of({"semantic_weight": 1.5})
    kg_graph.answer(Cache(), None, Handles(), (1, "p"), "p", None, None, "hub", [None], "kg_search", {}, tuning=tuned)
    assert seen["tuning"] == tuned


def test_a_measure_asks_with_the_current_and_the_candidate_tuning_and_leaves_build_keys_out(monkeypatch):
    from evo_agents.hub.server import kg_settings as route

    used = []

    class Opened:
        tuning = kg.DEFAULT

        def vector_info(self):
            return object()

        def close(self):
            pass

    class Cache:
        def acquire(self, store, project, graphs):
            return "path", SimpleNamespace(build_id=3)

        def release(self, path):
            pass

    class Questions:
        def part(self, split):
            return [SimpleNamespace(project=None, id=i) for i in range(3)]

    def score(questions, skipped, available, session, **kw):
        used.append((session.opened.tuning.semantic_weight, session.semantic, len(questions)))
        return {"methods": {"kg_search": {"n": len(questions), "hit@5": 0.5}}}

    monkeypatch.setattr(route, "open_graph", lambda *a: Opened())
    monkeypatch.setattr(route, "HubSession", lambda *a, **k: SimpleNamespace(opened=a[4], semantic=False))
    monkeypatch.setattr(route.kg_health_eval, "score", score)
    access = SimpleNamespace(rules=SimpleNamespace(ceiling=lambda *a: None, policy=None), max_level=None)
    current = kg.of({"semantic_mode": "index"})
    candidate = kg.of({"semantic_weight": 1.5, "semantic_mode": "on"}, current)
    found = route._measure(
        Cache(), None, "p", access, "cli", frozenset(), [None], (Questions(), "d"), current, candidate, "tune", 2
    )
    assert used == [(kg.DEFAULT.semantic_weight, False, 2), (1.5, True, 2)]
    assert found["build_id"] == 3 and found["questions"] == 2 and found["candidate"]["n"] == 2


def test_a_hub_build_reads_the_ontology_of_the_strategy_its_settings_name():
    """A strategy set on the hub changes no file of the harness: the build adds the strategy's node kinds and relations
    to the project's ontology itself, else the entities step of entity-graph-api would drop every relation."""
    own = {"node_kinds": ["Concept", "Work"], "relations": ["related_to"]}
    config = {"knowledge": {"version": 1, "ontology": "ontology.yaml"}, "ontology": own}
    knowledge, ontology = kg_build.knowledge_and_ontology(
        config, [], kg.of({"strategy": "entity-graph-api"}, kg.DEFAULT)
    )
    assert knowledge["ontology"] == "ontology.yaml"
    assert ontology["node_kinds"][:2] == ["Concept", "Work"] and "Artifact" in ontology["node_kinds"]
    assert ontology["relations"] == ["related_to", "relates_to"]
    assert own == {"node_kinds": ["Concept", "Work"], "relations": ["related_to"]}, "the config itself is not changed"

    bare = {"knowledge": {"version": 1}, "ontology": None}
    knowledge, ontology = kg_build.knowledge_and_ontology(bare, [], kg.of({"strategy": "entity-graph-api"}, kg.DEFAULT))
    assert knowledge["ontology"] == "ontology.yaml" and "relates_to" in ontology["relations"]

    for tuning in (None, kg.DEFAULT, kg.of({"strategy": "code"}, kg.DEFAULT), kg.of({"strategy": "gone"}, kg.DEFAULT)):
        assert kg_build.knowledge_and_ontology(bare, [], tuning) == ({"version": 1}, None)
