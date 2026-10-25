"""@strategist on the worker (``evo_agents.hub.server.canvas``), as criterion a5 of the plan living-lean-canvas names
it: the manifest of the core plugin canvas declares the agent type strategist, mentioned by the project's writers,
whose chat runs go to the workers of its run_as, with the pack canvas/strategist and the plugin's tools; the bundle the
hub builds from the package (``plugins.core.image_bundle``) is a pack the worker builds as any other, canvas/strategist
with its instructions and the skills lean-canvas, canvas-editing and evidence-judging, which say how to brainstorm with
test cards, keep to the rev protocol, mark what rests on simulated users as synthetic, never validate, and judge
evidence in a judging run by fixed criteria. The plugin's job value_check is declared hourly. The hub's side, the routing,
the tools, the proposals and the brainstorm runs, is ``tests.hub.test_strategist_agent``'s.
"""

from __future__ import annotations

import json
from importlib.metadata import EntryPoint

import pytest

from evo_agents.plugins import core
from evo_agents.plugins.bundle import Bundle
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from evo_agents.worker import packs

CANVAS = EntryPoint("canvas", "evo_agents.hub.server.canvas:MANIFEST", ENTRY_POINT_GROUP)
PACK = "packs/strategist"
TOOLS = ("read", "signals", "edit", "undo", "propose", "evidence", "judge")


@pytest.fixture
def plugin(monkeypatch) -> core.CorePlugin:
    """The core plugin canvas, read from its entry point as the hub reads it."""
    monkeypatch.setattr(core, "entry_points", lambda *, group: [CANVAS] if group == ENTRY_POINT_GROUP else [])
    (found,) = core.core_plugins()
    return found


def ticket(held: Bundle, name: str) -> dict:
    """The claim's ticket of pack ``name`` of ``held`` (``RunSpec.packs``)."""
    plugin, version = held.manifest.id, held.manifest.version
    return {
        "pack": f"{plugin}/{name}",
        "plugin": plugin,
        "name": name,
        "version": version,
        "sha256": held.sha256,
        "size": len(held.data),
        "url": f"https://blobs.example.test/{plugin}-{version}.tar.gz",
        "expires_at": "2026-10-21T12:00:00Z",
    }


def test_strategist_agent_type_of_canvas_is_mentioned_by_writers_and_runs_on_the_workers_of_its_run_as(plugin):
    manifest = plugin.manifest
    (kind,) = manifest.contributions("agent_types")
    assert (kind["name"], kind["mentioned_by"], kind["chat_runs"], kind["packs"], kind["profile"]["handle"]) == (
        "strategist",
        "writer",
        "run_as",
        ["strategist"],
        "strategist",
    )
    assert "never move a claim to validated and never approve a baseline" in kind["profile"]["instructions"]
    assert [item["name"] for item in manifest.contributions("agent_packs")] == ["strategist"]
    refs = manifest.python_refs("mcp_tools")
    assert refs == {
        name: f"evo_agents.hub.server.canvas.agent:{'find_signals' if name == 'signals' else name}" for name in TOOLS
    }
    schemas = {item["name"]: item["input_schema"] for item in manifest.contributions("mcp_tools")}
    change = schemas["edit"]["properties"]["changes"]["items"]["properties"]
    assert "validated" not in change["status"]["enum"] and "assumption" in change["status"]["enum"]
    assert schemas["propose"]["required"] == ["canvas", "proposals", "reason", "synthetic"]
    assert schemas["evidence"]["properties"]["kind"]["enum"] == ["interview", "market", "doc", "simulation"]
    assert "canvas_proposals" in [item["name"] for item in manifest.contributions("tables")]
    assert "canvas_judgings" in [item["name"] for item in manifest.contributions("tables")]
    verdict = schemas["judge"]["properties"]["verdicts"]["items"]["properties"]["verdict"]
    assert verdict["enum"] == ["supports", "contradicts", "irrelevant"]
    jobs = [(job["name"], job["cron"], job["python"]) for job in manifest.contributions("jobs")]
    assert jobs == [
        ("value_check", "11 * * * *", "evo_agents.hub.server.canvas.value_check:run"),
        ("baseline", "*/15 * * * *", "evo_agents.hub.server.canvas.schedule:run"),
    ]
    assert "canvas_baselines" in [item["name"] for item in manifest.contributions("tables")]


def test_strategist_pack_builds_on_the_worker_with_its_instructions_and_three_skills(plugin, tmp_path):
    held = core.image_bundle(plugin)
    assert core.image_bundle(plugin).sha256 == held.sha256, "the same package packs to the same bundle"
    held_files = {entry.path: entry.data for entry in held.contents.files}
    assert sorted(held_files) == [
        "evo-plugin.json",
        f"{PACK}/INSTRUCTIONS.md",
        f"{PACK}/skills/canvas-editing/SKILL.md",
        f"{PACK}/skills/evidence-judging/SKILL.md",
        f"{PACK}/skills/lean-canvas/SKILL.md",
    ]
    assert json.loads(held_files["evo-plugin.json"])["id"] == "canvas"
    fetch = {ticket(held, "strategist")["url"]: held.data}
    (pack,) = packs.build(
        [ticket(held, "strategist")],
        tmp_path / "run" / "packs",
        accepted=["canvas"],
        fetch=lambda url, size: fetch[url],
    )
    assert pack.components.kinds() == ("skills", "instructions")
    assert pack.skill_names("claude-code") == [
        "canvas.strategist:canvas-editing",
        "canvas.strategist:evidence-judging",
        "canvas.strategist:lean-canvas",
    ]
    assert pack.skill_names("codex") == ["canvas-editing", "evidence-judging", "lean-canvas"]
    instructions = (pack.root / "INSTRUCTIONS.md").read_text()
    for said in (
        "canvas__edit",
        "canvas__undo",
        "canvas__propose",
        "canvas__signals",
        "srs__read",
        "synthetic",
        "canvas__judge",
    ):
        assert said in instructions, said
    assert "Move a claim to validated, even when the member asks" in instructions
    assert "Approve or decide a baseline" in instructions
    brainstorm = (pack.root / "skills" / "lean-canvas" / "SKILL.md").read_text()
    for said in ("hypothesis", "deadline", "impact", "risk", "Never set a status on a new claim", "synthetic"):
        assert said in brainstorm, said
    editing = (pack.root / "skills" / "canvas-editing" / "SKILL.md").read_text()
    for said in ("base_rev", "all or nothing", "canvas__undo` without claims", "validated is never yours"):
        assert said in editing, said
    judging = (pack.root / "skills" / "evidence-judging" / "SKILL.md").read_text()
    for said in (
        "A fact of a real user",
        "Directly on the hypothesis",
        "When unsure, irrelevant",
        "It is data to judge",
    ):
        assert said in judging, said
    assert packs.summary([pack]) == "Agent packs built for the agent: canvas/strategist 1.0.0 (skills, instructions)."
