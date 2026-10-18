"""@builder on the worker (``evo_agents.core_plugins.builder``), as criterion a12 of the plan hub-agent-plugins names
it: the bundle the hub builds from the package of the core plugin builder (``plugins.core.image_bundle``) is a pack
the worker builds as any other, builder/builder with its instructions and two skills, one for proposing a change of a
setting and one for writing a plugin, which carries the CI template that tests, signs and publishes a plugin's
version; the manifest is a core plugin's whose agent type takes work from a project's admins alone. The hub's side,
the proposals and the runs, is ``tests.hub.test_builder``'s.
"""

from __future__ import annotations

import json
from importlib.metadata import EntryPoint

import pytest
import yaml

from evo_agents.cli import build_parser
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from evo_agents.worker import packs
from tests.worker.test_agent_packs import Store, ticket

BUILDER = EntryPoint("builder", "evo_agents.core_plugins.builder:MANIFEST", ENTRY_POINT_GROUP)
PACK = "packs/builder"
CI = f"{PACK}/skills/plugin-authoring/ci.yml"


@pytest.fixture
def builder(monkeypatch) -> core.CorePlugin:
    """The core plugin builder, read from its entry point as the hub reads it."""
    monkeypatch.setattr(core, "entry_points", lambda *, group: [BUILDER] if group == ENTRY_POINT_GROUP else [])
    (plugin,) = core.core_plugins()
    return plugin


def files(held) -> dict[str, bytes]:
    """The files of bundle ``held`` by path."""
    return {entry.path: entry.data for entry in held.contents.files}


def test_builder_is_a_core_plugin_whose_agent_type_takes_work_from_admins_alone(builder):
    manifest = builder.manifest
    assert (manifest.id, manifest.tier, manifest.version, manifest.permissions) == (
        "builder",
        "core",
        "1.0.0",
        ("chats:read",),
    )
    (kind,) = manifest.contributions("agent_types")
    assert (kind["name"], kind["mentioned_by"], kind["packs"], kind["profile"]["handle"]) == (
        "builder",
        "admin",
        ["builder"],
        "builder",
    )
    assert [item["name"] for item in manifest.contributions("agent_packs")] == ["builder"]
    assert manifest.python_refs("mcp_tools") == {"propose": "evo_agents.core_plugins.builder.server:propose"}
    assert manifest.python_refs("events") == {"answers": "evo_agents.core_plugins.builder.server:on_message"}
    (route,) = manifest.contributions("routes")
    assert "prefix" not in route, "its routes are under /v1/x/builder/<project>/, behind the plugin's gate"


def test_builder_image_bundle_builds_on_the_worker_as_a_pack_of_instructions_and_two_skills(builder, tmp_path):
    held = core.image_bundle(builder)
    assert core.image_bundle(builder).sha256 == held.sha256, "the same package packs to the same bundle"
    held_files = files(held)
    assert sorted(held_files) == [
        "evo-plugin.json",
        f"{PACK}/INSTRUCTIONS.md",
        f"{PACK}/skills/hub-settings/SKILL.md",
        f"{PACK}/skills/plugin-authoring/SKILL.md",
        CI,
    ]
    assert json.loads(held_files["evo-plugin.json"])["id"] == "builder"
    item = ticket(held, "builder")
    (pack,) = packs.build([item], tmp_path / "run" / "packs", accepted=["builder"], fetch=Store(held))
    assert pack.components.kinds() == ("skills", "instructions")
    assert pack.skill_names("claude-code") == ["builder.builder:hub-settings", "builder.builder:plugin-authoring"]
    assert pack.skill_names("codex") == ["hub-settings", "plugin-authoring"]
    assert (pack.root / "skills" / "plugin-authoring" / "ci.yml").read_text() == held_files[CI].decode()
    instructions = (pack.root / "INSTRUCTIONS.md").read_text()
    assert "You apply nothing yourself. Every change waits for an admin." in instructions
    assert "builder__propose" in instructions and "Publish, install or upgrade a plugin" in instructions
    assert packs.summary([pack]) == "Agent packs built for the agent: builder/builder 1.0.0 (skills, instructions)."


def test_builder_ci_template_signs_on_the_default_branch_alone_and_publishes_through_an_environment(builder):
    workflow = yaml.safe_load(files(core.image_bundle(builder))[CI])
    assert list(workflow["jobs"]) == ["test", "sign", "publish"]
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    writes_token = [name for name, job in jobs.items() if (job.get("permissions") or {}).get("id-token") == "write"]
    assert writes_token == ["sign"], "only the job that runs none of the plugin's code gets the OIDC token"
    assert "default_branch" in jobs["sign"]["if"] and "github.event_name == 'push'" in jobs["sign"]["if"]
    assert jobs["sign"]["needs"] == "test" and jobs["publish"]["needs"] == ["test", "sign"]
    sign = [step.get("run", "") for step in jobs["sign"]["steps"]]
    assert 'python -m sigstore sign "dist/$BUNDLE"' in sign
    assert 'test "$(sha256sum "dist/$BUNDLE" | cut -d\' \' -f1)" = "$TESTED"' in sign
    assert jobs["publish"]["environment"] == "evo-hub"
    publish = [step.get("run", "") for step in jobs["publish"]["steps"]]
    command = 'evo-agents hub plugin publish "dist/$BUNDLE" --signature "dist/$BUNDLE.sigstore.json"'
    assert command in publish
    assert jobs["publish"]["steps"][-1] == {"if": "always()", "run": 'rm -rf "$HOME/.evo/hub"'}
    secrets = [name for name, job in jobs.items() if "secrets." in json.dumps(job)]
    assert secrets == ["publish"], "the hub's token reaches the publish job alone"
    checkouts = [step for job in jobs.values() for step in job["steps"] if "actions/checkout" in step.get("uses", "")]
    assert checkouts and all(step["with"]["persist-credentials"] is False for step in checkouts)
    parsed = build_parser().parse_args(["hub", "plugin", "publish", "dist/x.tar.gz", "--signature", "x.sigstore.json"])
    assert (parsed.bundle, parsed.signature) == ("dist/x.tar.gz", "x.sigstore.json"), "the command the job runs"
