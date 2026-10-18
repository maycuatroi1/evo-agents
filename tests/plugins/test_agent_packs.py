"""Agent packs of plugins: a directory laid out as a Claude Code plugin, read into its components, and refused when
it holds what no runtime of the worker loads or what does not read; a bundle refuses a pack its manifest declares and
that does not read, so the hub refuses it when it is published."""

from __future__ import annotations

import json

import pytest

from evo_agents.plugins import agent_packs
from evo_agents.plugins.agent_packs import PackError, Server, components
from evo_agents.plugins.bundle import BundleError, pack, read_bundle
from evo_agents.plugins.manifest import ManifestError, is_per_project, read_manifest
from tests.plugins.samples import manifest, pack_files, write_plugin


def files(changes: dict[str, str] | None = None) -> dict[str, bytes]:
    """The files of the sample pack, by their paths inside it, with ``changes``."""
    return {path: text.encode() for path, text in {**pack_files(top=""), **(changes or {})}.items()}


def test_agent_pack_holds_each_kind_of_component_as_a_claude_code_plugin_lays_it_out():
    found = components(files())
    assert found.kinds() == agent_packs.KINDS
    assert [(skill.name, skill.description, skill.path) for skill in found.skills] == [
        ("review", "Review a diff for bugs", "skills/review/SKILL.md")
    ]
    assert found.agents == ("critic",)
    assert found.hooks
    assert [server.name for server in found.servers] == ["docs", "lint"]
    assert found.instructions == "Cite the line you review."
    assert found.resources == ("bin/lint", "skills/review/checklist.md")


def test_agent_pack_server_takes_the_directory_the_worker_builds_it_in():
    lint = next(server for server in components(files()).servers if server.name == "lint")
    rooted = lint.rooted("/runs/7/packs/sandbox/reviewer")
    assert rooted.claude() == {
        "type": "stdio",
        "command": "/runs/7/packs/sandbox/reviewer/bin/lint",
        "args": ["--fast"],
        "env": {"MODE": "strict"},
    }
    docs = Server("docs", "sse", url="https://x.test/mcp", headers=(("A", "b"),))
    assert docs.claude() == {"type": "sse", "url": "https://x.test/mcp", "headers": {"A": "b"}}


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"commands/go.md": "go"}, "commands is not one of them"),
        ({".claude-plugin/plugin.json": "{}"}, ".claude-plugin is not one of them"),
        ({"settings.json": "{}"}, "settings.json is not one of them"),
        ({"skills/Bad/SKILL.md": "---\nname: Bad\ndescription: x\n---\n"}, "lowercase letters"),
        ({"skills/solo.md": "x"}, "a skill is a directory skills/<skill>/"),
        ({"skills/other/notes.md": "x"}, "skills/other/ holds no SKILL.md"),
        ({"skills/review/SKILL.md": "---\nname: other\ndescription: x\n---\n"}, "names the skill review"),
        ({"skills/review/SKILL.md": "---\nname: review\n---\n"}, "describes the skill"),
        ({"skills/review/SKILL.md": "---\nname: review\ndescription: x\n"}, "never closes"),
        ({"agents/critic.txt": "x"}, "agents/<agent>.md"),
        ({"hooks/hooks.json": "[]"}, "holds list, not an object"),
        ({"hooks/hooks.json": '{"hooks": {}}'}, "an object of events"),
        ({".mcp.json": "{"}, "is not JSON"),
        ({".mcp.json": '{"servers": {}}'}, "only mcpServers"),
        ({".mcp.json": '{"mcpServers": {"a b": {"command": "x"}}}'}, "a server's name"),
        ({".mcp.json": '{"mcpServers": {"x": {"type": "ws"}}}'}, "type is one of stdio, http, sse"),
        ({".mcp.json": '{"mcpServers": {"x": {"command": ""}}}'}, "names its command"),
        ({".mcp.json": '{"mcpServers": {"x": {"command": "a", "cwd": "/"}}}'}, "not cwd"),
        ({".mcp.json": '{"mcpServers": {"x": {"command": "a", "args": "b"}}}'}, "args is a list of strings"),
        ({".mcp.json": '{"mcpServers": {"x": {"command": "a", "env": {"A": 1}}}}'}, "env maps names to strings"),
        ({".mcp.json": '{"mcpServers": {"x": {"type": "http", "url": "ftp://x"}}}'}, "http or https url"),
        ({"INSTRUCTIONS.md": "x" * (agent_packs.MAX_INSTRUCTIONS + 1)}, "over 32768"),
    ],
)
def test_agent_pack_is_refused_for_what_no_runtime_loads_or_what_does_not_read(changes, problem):
    with pytest.raises(PackError, match=problem):
        components(files(changes))
    with pytest.raises(PackError, match="INSTRUCTIONS.md is not UTF-8 text"):
        components({**files(), "INSTRUCTIONS.md": b"\xff"})


def test_agent_pack_with_nothing_a_runtime_loads_is_refused():
    with pytest.raises(PackError, match="the pack holds no skills/"):
        components({"bin/lint": b"#!/bin/sh\n"})
    assert components({"INSTRUCTIONS.md": b"Be careful.\n"}).kinds() == ("instructions",)
    many = {".mcp.json": json.dumps({"mcpServers": {f"s{i}": {"command": "x"} for i in range(21)}}).encode()}
    with pytest.raises(PackError, match="at most 20 servers"):
        components(many)


def test_agent_pack_front_matter_reads_plain_quoted_and_block_scalars():
    text = "---\nname: 'review'\ndescription: |\n  Two\n  lines\nother: \"x\"\n---\nbody\n"
    assert agent_packs.front_matter(text) == {"name": "review", "description": "Two lines", "other": "x"}
    assert agent_packs.front_matter("no front matter\n") == {}


def test_agent_pack_names_read_as_plugin_slash_pack():
    assert agent_packs.pack_name("sandbox", "reviewer") == "sandbox/reviewer"
    assert agent_packs.split_pack("sandbox/reviewer") == ("sandbox", "reviewer")
    for bad in ("sandbox", "sandbox/", "/reviewer", "Sandbox/reviewer", "a/b/c"):
        assert not agent_packs.is_pack_name(bad)
        with pytest.raises(ValueError):
            agent_packs.split_pack(bad)
    entries = [("packs/reviewer/INSTRUCTIONS.md", b"x"), ("packs/other/INSTRUCTIONS.md", b"y"), ("web/a", b"z")]
    assert agent_packs.files_of(entries, "reviewer") == {"INSTRUCTIONS.md": b"x"}


def test_agent_pack_a_manifest_declares_is_checked_when_the_bundle_is_read(tmp_path):
    data = manifest(contributes={"agent_packs": [{"name": "reviewer", "title": "Reviewer"}]})
    assert is_per_project(read_manifest(data).data)
    held = pack(write_plugin(tmp_path / "held", data, pack_files()))
    assert "packs/reviewer/skills/review/SKILL.md" in [entry.path for entry in read_bundle(held.data).files]
    with pytest.raises(BundleError, match=r"agent pack reviewer \(packs/reviewer/\): the pack holds no skills/"):
        pack(write_plugin(tmp_path / "missing", data, {"web/index.html": "<p>hi</p>\n"}))
    bad = {**pack_files(), "packs/reviewer/commands/go.md": "go\n"}
    with pytest.raises(BundleError, match="commands is not one of them"):
        pack(write_plugin(tmp_path / "bad", data, bad))


def test_agent_pack_contribution_takes_only_its_name_title_description_and_api():
    for extra in ({"python": "a.b:c"}, {"path": "elsewhere"}):
        data = manifest(contributes={"agent_packs": [{"name": "reviewer", **extra}]})
        with pytest.raises(ManifestError, match=rf"contributes.agent_packs\[0\].{next(iter(extra))}: unknown key"):
            read_manifest(data)
    with pytest.raises(ManifestError, match=r"contributes.agent_packs\[0\].name"):
        read_manifest(manifest(contributes={"agent_packs": [{"name": "Reviewer"}]}))
