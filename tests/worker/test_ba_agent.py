"""@ba on the worker (``evo_agents.core_plugins.srs``), as criterion a7 of the plan srs-tasks-plugins names it: the
manifest of the core plugin srs declares the agent type ba, mentioned by the project's writers, whose chat runs go to
the workers of its run_as, with the pack srs/ba and the plugin's tools; the bundle the hub builds from the package
(``plugins.core.image_bundle``) is a pack the worker builds as any other, srs/ba with its instructions and the skills
srs-writing and srs-review, which say how to propose, review and keep codes. The hub's side, the routing, the tools
and the review runs, is ``tests.hub.test_ba_agent``'s.
"""

from __future__ import annotations

import json
from importlib.metadata import EntryPoint

import pytest

from evo_agents.plugins import core
from evo_agents.plugins.bundle import Bundle
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from evo_agents.worker import packs

SRS = EntryPoint("srs", "evo_agents.core_plugins.srs:MANIFEST", ENTRY_POINT_GROUP)
TASKS = EntryPoint("tasks", "evo_agents.core_plugins.tasks:MANIFEST", ENTRY_POINT_GROUP)
PACK = "packs/ba"


@pytest.fixture
def plugins(monkeypatch) -> dict[str, core.CorePlugin]:
    """The core plugins srs and tasks, read from their entry points as the hub reads them."""
    monkeypatch.setattr(core, "entry_points", lambda *, group: [SRS, TASKS] if group == ENTRY_POINT_GROUP else [])
    return {plugin.manifest.id: plugin for plugin in core.core_plugins()}


def ticket(held: Bundle, name: str) -> dict:
    """The claim's ticket of pack ``name`` of ``held`` (``RunSpec.packs``), as ``tests.worker.test_agent_packs`` has
    it; that module needs the worker extra, which this check of the pack's files does not."""
    plugin, version = held.manifest.id, held.manifest.version
    return {
        "pack": f"{plugin}/{name}",
        "plugin": plugin,
        "name": name,
        "version": version,
        "sha256": held.sha256,
        "size": len(held.data),
        "url": f"https://blobs.example.test/{plugin}-{version}.tar.gz",
        "expires_at": "2026-10-19T12:00:00Z",
    }


def fetch_from(held: Bundle):
    """The blob store of the test: the bundle's bytes at its ticket's URL."""
    return lambda url, size: {ticket(held, "ba")["url"]: held.data}[url]


def files(held) -> dict[str, bytes]:
    return {entry.path: entry.data for entry in held.contents.files}


def test_ba_agent_type_of_srs_is_mentioned_by_writers_and_runs_on_the_workers_of_its_run_as(plugins):
    manifest = plugins["srs"].manifest
    (kind,) = manifest.contributions("agent_types")
    assert (kind["name"], kind["mentioned_by"], kind["chat_runs"], kind["packs"], kind["profile"]["handle"]) == (
        "ba",
        "writer",
        "run_as",
        ["ba"],
        "ba",
    )
    assert "never approve" in kind["profile"]["instructions"]
    assert [item["name"] for item in manifest.contributions("agent_packs")] == ["ba"]
    tools = manifest.python_refs("mcp_tools")
    assert tools == {
        name: f"evo_agents.core_plugins.srs.agent:{name}"
        for name in ("read", "search", "diff", "propose", "comment", "finding")
    }
    propose = next(item for item in manifest.contributions("mcp_tools") if item["name"] == "propose")
    change = propose["input_schema"]["properties"]["changes"]["items"]
    assert "retire" not in change["properties"] and change["additionalProperties"] is False, "no change drops an item"
    assert "code" not in change["properties"], "a change of an item never names its code"
    assert plugins["tasks"].manifest.python_refs("mcp_tools") == {
        "create": "evo_agents.core_plugins.tasks.agent:create"
    }


def test_ba_agent_pack_builds_on_the_worker_with_its_instructions_and_two_skills(plugins, tmp_path):
    held = core.image_bundle(plugins["srs"])
    assert core.image_bundle(plugins["srs"]).sha256 == held.sha256, "the same package packs to the same bundle"
    held_files = files(held)
    assert sorted(held_files) == [
        "evo-plugin.json",
        f"{PACK}/INSTRUCTIONS.md",
        f"{PACK}/skills/srs-review/SKILL.md",
        f"{PACK}/skills/srs-writing/SKILL.md",
    ]
    assert json.loads(held_files["evo-plugin.json"])["id"] == "srs"
    (pack,) = packs.build([ticket(held, "ba")], tmp_path / "run" / "packs", accepted=["srs"], fetch=fetch_from(held))
    assert pack.components.kinds() == ("skills", "instructions")
    assert pack.skill_names("claude-code") == ["srs.ba:srs-review", "srs.ba:srs-writing"]
    assert pack.skill_names("codex") == ["srs-review", "srs-writing"]
    instructions = (pack.root / "INSTRUCTIONS.md").read_text()
    for said in ("srs__propose", "srs__comment", "tasks__create", "Approve or return a version"):
        assert said in instructions, said
    writing = (pack.root / "skills" / "srs-writing" / "SKILL.md").read_text()
    assert "Never change, renumber or reuse the code of an item" in writing
    review = (pack.root / "skills" / "srs-review" / "SKILL.md").read_text()
    assert "Blocking:" in review and "with the glossary" in review and "non-functional requirements" in review
    assert packs.summary([pack]) == "Agent packs built for the agent: srs/ba 1.0.0 (skills, instructions)."
