"""The versioned write and the tool table of the hub server's base (``evo_agents.hub.server.base.versions``,
``evo_agents.hub.server.base.tools``): a write made on the version now writes the next one, one made on another is
refused with what the store says of it, values equal to the record's write nothing; the table gives the manifest's
``mcp_tools`` in order, each pointing at its handler."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi", reason="needs the hub-server extra, evo-ak[hub-server]")
pytest.importorskip("sqlalchemy", reason="needs the hub-server extra, evo-ak[hub-server]")

from evo_agents.hub.server.base.errors import Conflict
from evo_agents.hub.server.base.tools import Tool, Tools
from evo_agents.hub.server.base.versions import VersionedStore


class Notes(VersionedStore[str, dict]):
    noun = "note"

    def __init__(self):
        self.rows: dict[str, dict] = {}

    async def held(self, conn, key):
        return self.rows.get(key)

    def version_of(self, row):
        return row["version"]

    def unchanged(self, row, values):
        return row["text"] == values

    async def insert(self, conn, key, row, version, values):
        self.rows[key] = {"version": version, "text": values}


def _save(store, key, base, text):
    async def values_of(row):
        return text

    return asyncio.run(store.save(None, key, base, values_of))


def test_save_writes_the_next_version_on_the_version_now():
    store = Notes()
    first = _save(store, "a", None, "one")
    assert (first.before, first.version, first.changed) == (None, 1, True)
    second = _save(store, "a", 1, "two")
    assert (second.version, second.changed, store.rows["a"]) == (2, True, {"version": 2, "text": "two"})


def test_save_of_the_same_values_writes_nothing():
    store = Notes()
    _save(store, "a", None, "one")
    same = _save(store, "a", 1, "one")
    assert (same.version, same.changed) == (1, False)


def test_a_stale_base_is_refused_and_writes_nothing():
    store = Notes()
    _save(store, "a", None, "one")
    _save(store, "a", 1, "two")
    with pytest.raises(Conflict, match="note a is at version 2, not 1; nothing changed"):
        _save(store, "a", 1, "three")
    with pytest.raises(Conflict, match="there is no note b"):
        _save(store, "b", 3, "x")
    assert store.rows["a"]["text"] == "two"


def test_check_all_names_every_stale_record():
    store = Notes()
    for key in "abc":
        _save(store, key, None, key)
    _save(store, "b", 1, "bb")
    _save(store, "c", 1, "cc")
    with pytest.raises(Conflict) as refused:
        asyncio.run(store.check_all(None, store.rows, {"a": 1, "b": 1, "c": 1}))
    assert refused.value.detail == "note b is at version 2, not 1; note c is at version 2, not 1; nothing changed"


def test_tools_give_the_manifest_contributions_in_order():
    tools = Tools("pkg.agent", Tool("read", "Read", "reads", {"type": "object"}), Tool("find", "F", "f", {}, "found"))
    assert tools.names == ["read", "find"] and "find" in tools and len(tools) == 2
    assert [c["python"] for c in tools.contributions()] == ["pkg.agent:read", "pkg.agent:found"]
    assert tools["read"].contribution("m") == {
        "name": "read",
        "title": "Read",
        "description": "reads",
        "python": "m:read",
        "input_schema": {"type": "object"},
    }
    with pytest.raises(ValueError):
        Tools("m", Tool("a", "", "", {}), Tool("a", "", "", {}))
