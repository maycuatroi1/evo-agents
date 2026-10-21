"""Core plugins: Python packages that name a manifest in the entry point group evo_agents.plugins. The registries of
runtime adapters and of kg connectors read their contributions beside their own entry point groups, which keep
working and win over them; an entry point that does not load, is not a core plugin's or does not run on this release
is logged and left out."""

from __future__ import annotations

import logging
from importlib.metadata import EntryPoint

import pytest

from evo_agents import harness
from evo_agents.kg.protocol import runner
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP, core_plugins, python_contributions
from evo_agents.worker import adapter
from tests.plugins import core_sample

SAMPLE = "tests.plugins.core_sample"


def point(name: str, attribute: str, group: str = ENTRY_POINT_GROUP) -> EntryPoint:
    return EntryPoint(name, f"{SAMPLE}:{attribute}", group)


@pytest.fixture
def installed(monkeypatch):
    """Entry points by group, as installed distributions would declare them."""
    groups: dict[str, list[EntryPoint]] = {}

    def entry_points(*, group: str):
        return list(groups.get(group, []))

    for module in (core, adapter):
        monkeypatch.setattr(module, "entry_points", entry_points)
    monkeypatch.setattr(runner.metadata, "entry_points", entry_points)
    monkeypatch.setattr(harness.metadata, "entry_points", entry_points)
    monkeypatch.delenv(adapter.ADAPTERS_VARIABLE, raising=False)
    return groups


def test_plugin_core_entry_points_give_core_plugins_and_leave_out_the_rest(installed, caplog):
    installed[ENTRY_POINT_GROUP] = [
        point("b-callable", "manifest"),
        point("a-sample", "MANIFEST"),
        point("c-isolated", "ISOLATED"),
        point("d-too-new", "INCOMPATIBLE"),
        point("e-list", "NOT_A_DICT"),
        point("f-broken", "BROKEN"),
        point("g-missing", "NOWHERE"),
    ]
    with caplog.at_level(logging.WARNING, logger=core.__name__):
        found = core_plugins()
    assert [(plugin.entry_point, plugin.manifest.id) for plugin in found] == [
        ("a-sample", "sample-core"),
        ("b-callable", "sample-callable"),
    ]
    left_out = {record.entry_point: record.error for record in caplog.records}
    assert sorted(left_out) == ["c-isolated", "d-too-new", "e-list", "f-broken", "g-missing"]
    assert "is not a core plugin (tier isolated)" in left_out["c-isolated"]
    assert "too-new 1.0.0 does not run here: it runs on evo-agents >=99" in left_out["d-too-new"]
    assert "gives list, not a manifest" in left_out["e-list"]
    assert "version:" in left_out["f-broken"] and left_out["g-missing"].startswith("AttributeError")
    assert python_contributions("kg_connectors", found) == {"sample": f"{SAMPLE}:records", "late": "json:dumps"}


def test_plugin_core_registries_of_runtimes_and_connectors_read_the_new_entry_point_and_the_old(installed, caplog):
    assert adapter.adapter_specs({}) == adapter.BUILTIN  # nothing installed: the runtimes of the package
    installed[ENTRY_POINT_GROUP] = [point("sample", "MANIFEST")]
    specs = adapter.adapter_specs({})
    assert specs["codex"] == f"{SAMPLE}:SampleAdapter" and specs["claude-code"] == adapter.BUILTIN["claude-code"]
    assert adapter.load_adapters({})["codex"] is core_sample.SampleAdapter
    # The old group wins over a core plugin, and EVO_WORKER_ADAPTERS over both, as before.
    installed[adapter.ENTRY_POINT_GROUP] = [EntryPoint("codex", "tests.worker.fake_adapter:FakeAdapter", "x")]
    assert adapter.adapter_specs({})["codex"] == "tests.worker.fake_adapter:FakeAdapter"
    variable = {adapter.ADAPTERS_VARIABLE: "codex=pkg.mine:Mine"}
    assert adapter.adapter_specs(variable)["codex"] == "pkg.mine:Mine"

    assert "sample" in harness.available_connectors()
    assert runner.load_connector("sample") is core_sample.records
    installed[harness.CONNECTOR_GROUP] = [EntryPoint("old", f"{SAMPLE}:records", harness.CONNECTOR_GROUP)]
    assert {"sample", "old"} <= harness.available_connectors()
    assert runner.load_connector("old") is core_sample.records
    assert runner.load_connector("git") is not None  # the builtins first, as before

    installed[ENTRY_POINT_GROUP] = [point("sample", "MANIFEST"), EntryPoint("other", "json:loads", ENTRY_POINT_GROUP)]
    with caplog.at_level(logging.WARNING, logger=core.__name__):
        assert python_contributions("runtimes") == {"codex": f"{SAMPLE}:SampleAdapter"}


def test_plugin_core_contribution_that_does_not_import_is_a_connector_error(installed, monkeypatch):
    broken = {**core_sample.MANIFEST, "contributes": {"kg_connectors": [{"name": "gone", "python": "nowhere.at:all"}]}}
    monkeypatch.setattr(core_sample, "BROKEN_IMPORT", broken, raising=False)
    installed[ENTRY_POINT_GROUP] = [point("broken", "BROKEN_IMPORT")]
    with pytest.raises(runner.ConnectorError, match=r"cannot load connector 'gone' \(nowhere.at:all\)"):
        runner.load_connector("gone")


def test_plugin_core_two_plugins_with_one_name_keep_the_last_and_say_so(installed, caplog):
    second = {**core_sample.MANIFEST, "id": "second"}
    second["contributes"] = {"kg_connectors": [{"name": "sample", "python": "json:dumps"}]}
    first = core.CorePlugin("a", "x:y", core.read_manifest(core_sample.MANIFEST))
    last = core.CorePlugin("b", "x:z", core.read_manifest(second))
    with caplog.at_level(logging.WARNING, logger=core.__name__):
        assert python_contributions("kg_connectors", [first, last]) == {"sample": "json:dumps"}
    [clash] = [record for record in caplog.records if record.msg == "two core plugins contribute one name"]
    assert clash.plugins == ["sample-core", "second"]
