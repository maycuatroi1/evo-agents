"""The extension point rankers, version 1 (plan jev-decisions, criterion a3): a core plugin's manifest contributes the
rankers the hub's searches call; only a core plugin contributes one, it declares the egress sink the hits go through,
and a manifest without the point reads as before (seam plugin-v1 only gains)."""

from __future__ import annotations

import pytest

from evo_agents.plugins import compat
from evo_agents.plugins.manifest import ManifestError, is_per_project, read_manifest, schema
from tests.plugins.samples import manifest

EGRESS = [{"id": "ranker@northwind", "kind": "llm-backend", "host": "api.ranker.northwind.example"}]
RANKER = {"name": "rank", "python": "northwind_ranker:rank"}
CORE = manifest(id="northwind-ranker", tier="core", permissions=None, config=None, contributes={})


def test_ranker_plugin_manifest_point_has_a_version_and_a_schema():
    assert compat.POINT_VERSIONS["rankers"] == (1,)
    assert "rankers" in schema()["$defs"]["contributes"]["properties"]
    assert compat.problem(CORE["engines"], 1, contributes={"rankers": [{**RANKER, "api": 2}]}) is not None
    assert compat.problem(CORE["engines"], 1, contributes={"rankers": [RANKER]}) is None


def test_ranker_plugin_manifest_of_a_core_plugin_reads_and_a_project_enables_it():
    found = read_manifest({**CORE, "egress": EGRESS, "contributes": {"rankers": [RANKER]}})
    assert found.contributions("rankers") == (RANKER,) and found.per_project()
    assert is_per_project(found.data)


def test_ranker_plugin_manifest_is_additive_a_manifest_without_the_point_reads_as_before():
    assert read_manifest(manifest()).contributions("rankers") == ()


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (manifest(contributes={"rankers": [RANKER]}), "only a core plugin"),
        ({**CORE, "contributes": {"rankers": [RANKER]}}, "declares the sink in egress"),
        ({**CORE, "egress": EGRESS, "contributes": {"rankers": [{"name": "rank"}]}}, "python"),
        (
            {**CORE, "egress": EGRESS, "contributes": {"rankers": [{**RANKER, "sink": "other@northwind"}]}},
            "not a sink the plugin declares",
        ),
        (
            {
                **CORE,
                "egress": [*EGRESS, {**EGRESS[0], "id": "second@northwind"}],
                "contributes": {"rankers": [RANKER]},
            },
            "name the one it ranks through",
        ),
        ({**CORE, "egress": EGRESS, "contributes": {"rankers": [RANKER, RANKER]}}, "contributed twice"),
    ],
)
def test_ranker_plugin_manifest_refuses_what_is_not_a_ranker(data, problem):
    with pytest.raises(ManifestError, match=problem):
        read_manifest(data)
