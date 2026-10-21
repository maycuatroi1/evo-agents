"""Core plugins the tests install as entry points of group evo_agents.plugins (``test_plugin_core``)."""

from __future__ import annotations

from evo_agents.worker.adapter import Adapter


class SampleAdapter(Adapter):
    """A runtime adapter a core plugin contributes; the registry loads its class, nothing runs it."""


def records(context) -> list[dict]:
    """A kg connector a core plugin contributes: one node."""
    return [{"kind": "node", "id": f"sample:{context.source_id}"}]


def _core(**changes) -> dict:
    data = {
        "id": "sample-core",
        "version": "1.0.0",
        "publisher": "evo",
        "tier": "core",
        "engines": {"evo-agents": ">=0.16"},
        "plugin_api": 1,
        "contributes": {
            "runtimes": [{"name": "codex", "python": "tests.plugins.core_sample:SampleAdapter"}],
            "kg_connectors": [{"name": "sample", "python": "tests.plugins.core_sample:records"}],
        },
    }
    return data | changes


MANIFEST = _core()


def manifest() -> dict:
    """The same manifest, from a function, as an entry point may name either."""
    return _core(id="sample-callable", contributes={"kg_connectors": [{"name": "late", "python": "json:dumps"}]})


ISOLATED = _core(id="not-core", tier="isolated", contributes={})
INCOMPATIBLE = _core(id="too-new", engines={"evo-agents": ">=99"})
NOT_A_DICT = ["sample"]
BROKEN = _core(version="one")
