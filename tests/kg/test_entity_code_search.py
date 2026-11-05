"""The ``entity_code`` setting of kg_search and kg_context: off by default, a query key every surface takes. On, an
entity the question matches gives its place to the documents that mention it, then to the code it points to
(``implemented_by``, ``refers_to``), and the walk weighs ``implemented_by``. Off, both rank on the same graph exactly as
before, walk or no walk (the walk gives ``implemented_by`` no weight)."""

import json

import pytest

from evo_agents.cli import main
from evo_agents.kg import ppr, search
from evo_agents.kg import settings as kg_settings
from evo_agents.kg.serve import Session
from evo_agents.kg.store import Store
from tests.kg.test_walk import ALPHA, BETA, QUASAR, entity_project, walk_on

CODE = "app:file:app/search.py"


def link_code(project, entity=QUASAR, target=CODE, rel="implemented_by", conf=0.9, lvl=None):
    """An edge ``rel`` from ``entity`` to ``target`` in the latest build, labelled as one of the entity's mentions."""
    store = Store.for_project(project)
    assert store.nodes([target], store.latest_ready()), target
    with store.db:
        store.db.execute(
            "INSERT INTO edges SELECT ?, ?, ?, ?, props, 'extracted', ?, COALESCE(?, label_lvl), label_loc, label_int,"
            " content, tx_from, tx_to, label_aud FROM edges WHERE dst = ? AND rel = 'mentions' AND tx_to IS NULL"
            " LIMIT 1",
            (f"{entity}|{rel}|{target}", entity, rel, target, conf, lvl, entity),
        )


def set_code(project, on: bool):
    tuning = kg_settings.read(project.root)
    kg_settings.write(project.root, kg_settings.of({"entity_code": on}, tuning))


def call(project, tool, args, sink="agent"):
    found = Session(project, sink).call(tool, args)
    assert not found.get("isError"), found
    return found


def results(found) -> list[str]:
    return [r["id"] for r in found["structuredContent"]["results"]]


def test_entity_code_is_off_by_default_and_a_query_key_every_surface_takes(tmp_path):
    from evo_agents.kg import cli_bench

    assert kg_settings.DEFAULT.entity_code is False and "entity_code" in kg_settings.KEYS
    assert "entity_code" not in kg_settings.BUILD_KEYS
    on = kg_settings.of({"entity_code": True})
    assert on.entity_code is True and on.overrides() == {"entity_code": True}
    assert kg_settings.rebuild_keys(kg_settings.DEFAULT, on) == []  # measured on the newest build
    assert kg_settings.of(json.loads(json.dumps(on.overrides()))) == on  # as the hub's kg_settings row holds it
    assert kg_settings.parse_value("entity_code", "true") is True
    assert kg_settings.parse_value("entity_code", "off") is False
    with pytest.raises(kg_settings.SettingsError, match="entity_code"):
        kg_settings.of({"entity_code": 3})
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"settings": {"entity_code": True}}))
    assert cli_bench._tuning_file(str(path)) == {"entity_code": True}  # kg bench run --settings


def test_kg_settings_sets_entity_code(tmp_path, kg_env, capsys):
    from tests.kg.projects import make_project

    project = make_project(tmp_path, kg_env)
    argv = ["kg", "settings", "--project", str(project.harness.root), "--json"]
    assert main([*argv, "--set", "entity_code=true", "--reason", "trial"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["settings"]["entity_code"] is True and out["needs_rebuild"] == []
    assert main([*argv, "--set", "entity_code=maybe"]) == 2
    assert "entity_code" in capsys.readouterr().err


def test_the_walk_weighs_implemented_by_only_under_entity_code():
    edge = {"rel": "implemented_by", "status": "extracted", "conf": 0.9}
    assert ppr.weight(edge, ppr.of(True)) == 0.0
    assert ppr.weight(edge, ppr.of(True, entity_code=True)) == pytest.approx(ppr.ENTITY_CODE_WEIGHT * 0.9)
    assert ppr.of(True) == ppr.Walk() and "entity_code" not in ppr.KEYS  # not a key of the walk setting
    assert "entity_code" not in kg_settings.of({"walk": True, "entity_code": True}).walk


@pytest.mark.parametrize("walk", [False, True])
def test_off_ranks_as_before_on_the_same_graph(tmp_path, kg_env, walk):
    project = entity_project(tmp_path, kg_env)
    if walk:
        walk_on(project)
    queries = ("quasar", "zephyr launcher", "rank results search")
    before = [call(project, "kg_search", {"query": q})["structuredContent"] for q in queries]
    context = call(project, "kg_context", {"query": "quasar"})["structuredContent"]
    link_code(project)
    if not walk:  # the walk weighed refers_to (DEFAULT_REL) before the plan: only through_entities ignores it off
        link_code(project, rel="refers_to", target="app:file:app/server.py")
    for query, was in zip(queries, before, strict=True):
        now = call(project, "kg_search", {"query": query})["structuredContent"]
        assert results({"structuredContent": now}) == results({"structuredContent": was}), query
        assert now["entities"] == was["entities"]
    assert call(project, "kg_context", {"query": "quasar"})["structuredContent"]["seeds"] == context["seeds"]


@pytest.mark.parametrize("walk", [False, True])
def test_on_a_matched_entity_brings_its_documents_then_its_code(tmp_path, kg_env, walk):
    project = entity_project(tmp_path, kg_env)
    if walk:
        walk_on(project)
    link_code(project)
    set_code(project, True)
    found = call(project, "kg_search", {"query": "quasar"})
    ids = results(found)
    assert set(ids[:2]) == {ALPHA, BETA}  # the documents first
    assert CODE in ids and ids.index(CODE) > max(ids.index(ALPHA), ids.index(BETA))
    assert QUASAR not in ids or ids.index(QUASAR) > ids.index(CODE)  # the entity after its code
    assert found["structuredContent"]["entities"][CODE] == [QUASAR]
    context = call(project, "kg_context", {"query": "quasar"})["structuredContent"]
    assert CODE in context["seeds"] and context["entities"][CODE] == [QUASAR]


def test_on_an_entity_never_brings_code_the_reader_does_not_see(tmp_path, kg_env):
    from tests.kg.test_walk import hide

    project = entity_project(tmp_path, kg_env)
    link_code(project)
    set_code(project, True)
    hide(project, "UPDATE edges SET label_lvl = 2 WHERE src = ? AND rel = 'implemented_by'", QUASAR)
    assert CODE in results(call(project, "kg_search", {"query": "quasar"}, sink="wide"))
    for tool in ("kg_search", "kg_context"):
        found = call(project, tool, {"query": "quasar"})
        assert CODE not in found["structuredContent"].get("entities", {})


def test_code_of_an_entity_follows_its_documents_up_to_entity_code():
    entity = "entity:Component:ranker"
    code = [f"repo:file:src/{i}.py" for i in range(search.ENTITY_CODE + 2)]

    class Store:
        def edges_of(self, node_id, b, direction, limit):
            if direction == "in":
                return [{"src": "doc:a", "rel": "mentions", "dst": node_id, "conf": 0.9}]
            out = [
                {"src": node_id, "rel": "implemented_by", "dst": c, "conf": 0.5 + i / 100} for i, c in enumerate(code)
            ]
            return out + [{"src": node_id, "rel": "relates_to", "dst": "entity:Component:other", "conf": 1.0}]

        def nodes(self, ids, b):
            return {i: {"id": i, "kind": "File"} for i in ids}

    hits = [{"id": entity}]
    assert search.through_entities(Store(), 0, hits, lambda _: True)[0] == [{"id": "doc:a", "kind": "File"}, *hits]
    out, why = search.through_entities(Store(), 0, hits, lambda _: True, code=True)
    want = ["doc:a", *reversed(code)][: 1 + search.ENTITY_CODE]  # by confidence, the highest first
    assert [n["id"] for n in out] == [*want, entity]
    assert all(why[i] == [entity] for i in want)
