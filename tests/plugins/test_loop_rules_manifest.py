"""The extension point loop_rules, version 1 (plan autonomous-oss-growth, criterion a12): a core plugin's manifest
declares the rules it adds to the Loop; the point is additive to seam plugin-v1, so a manifest without it reads as
before; only a core plugin contributes it, and a project enables it."""

from __future__ import annotations

import pytest

from evo_agents.plugins import compat
from evo_agents.plugins.manifest import ManifestError, is_per_project, read_manifest, schema
from tests.plugins.samples import manifest

RULE = {
    "name": "ideas",
    "python": "tests.hub.loop_plugin:ideas",
    "on": ["canvas.baseline", "plugin.growth.idea_seen"],
    "emits": ["idea_seen"],
    "schedule": True,
    "kinds": [
        {"kind": "growth_idea", "run": "job"},
        {"kind": "growth_draft", "run": "task", "agent_type": "marketer"},
    ],
}
CORE = manifest(id="growth", tier="core", permissions=None, config=None, contributes={})


def test_loop_rules_plugin_manifest_point_has_a_version_and_a_schema():
    assert compat.POINT_VERSIONS["loop_rules"] == (1,)
    assert list(schema()["$defs"]["contributes"]["properties"])[-1] == "loop_rules"
    assert compat.problem(CORE["engines"], 1, contributes={"loop_rules": [{**RULE, "api": 2}]}) is not None
    assert compat.problem(CORE["engines"], 1, contributes={"loop_rules": [RULE]}) is None


def test_loop_rules_plugin_manifest_of_a_core_plugin_reads_and_a_project_enables_it():
    found = read_manifest({**CORE, "contributes": {"loop_rules": [RULE]}})
    assert found.contributions("loop_rules") == (RULE,) and found.per_project()
    assert is_per_project(found.data)


def test_loop_rules_plugin_manifest_is_additive_a_manifest_without_the_point_reads_as_before():
    plain = manifest()
    assert read_manifest(plain).contributions("loop_rules") == ()
    assert not is_per_project({**CORE})  # a core plugin with nothing per project stays hub-wide


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (manifest(contributes={"loop_rules": [RULE]}), "only a core plugin runs Python"),
        ({**CORE, "contributes": {"loop_rules": [{**RULE, "on": [], "schedule": False}]}}, "reads at least one event"),
        (
            {**CORE, "contributes": {"loop_rules": [{**RULE, "kinds": [{"kind": "growth_draft", "run": "task"}]}]}},
            "names the agent_type",
        ),
        ({**CORE, "contributes": {"loop_rules": [{**RULE, "kinds": [{"kind": "build", "run": "job"}]}]}}, "build"),
        (
            {
                **CORE,
                "contributes": {
                    "loop_rules": [{**RULE, "kinds": [{"kind": "growth_idea", "run": "job", "agent_type": "x"}]}]
                },
            },
            "has no agent_type",
        ),
        ({**CORE, "contributes": {"loop_rules": [RULE, RULE]}}, "contributed twice"),
        ({**CORE, "contributes": {"loop_rules": [{k: v for k, v in RULE.items() if k != "python"}]}}, "python"),
    ],
)
def test_loop_rules_plugin_manifest_refuses_a_rule_that_is_not_one(data, problem):
    with pytest.raises(ManifestError, match=problem):
        read_manifest(data)
