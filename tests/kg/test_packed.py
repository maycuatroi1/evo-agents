"""What a build holds between its stages, and for how long (``evo_agents.kg.packed``): the facts of every item packed,
not as objects, and a model a hub build loaded only for itself dropped after it. Both had a build of some 25,000 items
past a kg worker's memory cap."""

from dataclasses import replace

import pytest

pytest.importorskip("sqlalchemy", reason="a hub build needs the hub-server extra, evo-ak[hub-server]")

from evo_agents.hub import kg_semantic  # noqa: E402
from evo_agents.kg import build, semantic  # noqa: E402
from evo_agents.kg.packed import Packed  # noqa: E402
from evo_agents.kg.snapshot import StoreRows  # noqa: E402
from tests.kg import built  # noqa: E402
from tests.kg.built import sync_build  # noqa: E402
from tests.kg.test_hybrid import FAKE, Words, with_garage  # noqa: E402


def rows_of(project) -> StoreRows:
    """The rows of the project's latest build, without vectors."""
    return replace(built.rows_of(project), vectors=(), vector_info=None)


def test_a_packed_value_reads_back_equal_and_a_fresh_copy_each_time():
    held = Packed()
    facts = [{"t": "node", "id": "lumen-app:docs/garage.md", "attrs": {"tags": ["car"]}}]
    held["a"] = facts
    assert held["a"] == facts and held.get("b", []) == [] and list(held) == ["a"] and len(held) == 1
    got = held["a"]
    got[0]["attrs"]["tags"].append("night")
    assert held["a"] == facts and held["a"] is not held["a"]
    assert 0 < held.size()


def test_a_build_holds_the_facts_of_its_items_packed(tmp_path, kg_env, monkeypatch):
    project = with_garage(tmp_path, kg_env)
    seen = {}
    map_items, link_items = build.map_items, build.link_items

    def mapped(*a, **kw):
        seen["map"] = found = map_items(*a, **kw)
        return found

    def linked(*a, **kw):
        seen["link"] = found = link_items(*a, **kw)
        return found

    monkeypatch.setattr(build, "map_items", mapped)
    monkeypatch.setattr(build, "link_items", linked)
    report = sync_build(project)
    first, structure_out, _ = seen["map"]
    assert isinstance(first, Packed) and isinstance(seen["link"], Packed)
    assert all(isinstance(held, Packed) for held, _ in structure_out.values())
    assert len(first) > 0 and report.nodes > 0


def test_a_hub_build_drops_the_model_it_loaded_and_keeps_one_already_loaded(tmp_path, kg_env, monkeypatch):
    semantic.forget_models()
    monkeypatch.setitem(semantic.FACTORIES, FAKE, Words)
    project = with_garage(tmp_path, kg_env)
    sync_build(project)
    cache = semantic.MemoryCache()
    try:
        _, done = kg_semantic.index(rows_of(project), semantic.Settings("index", FAKE), cache)
        assert done.rows > 0 and done.embedded == done.rows and not done.error
        assert not semantic.is_loaded(FAKE)
        semantic.embedder(FAKE)
        kg_semantic.index(rows_of(project), semantic.Settings("index", FAKE), cache)
        assert semantic.is_loaded(FAKE)
    finally:
        semantic.forget_models()
