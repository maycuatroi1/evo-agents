"""The release is named in three places: evo_agents/__init__.py and the plugin.json of evo-kg and evo-hub, which
carry ``evo_agents.__version__`` and which scripts/bump_version.py moves in one go. The plugins pin no package: their
MCP servers and hooks run ``evo-agents`` from the PATH, the CLI the machine installed, so .mcp.json and hooks.json name
no version. Nothing else names the release: the marketplace entries leave the version to plugin.json, whose version
wins over an entry's; web/package.json has none, since images.yml tags the hub-web image from the git tag; and the
README, deploy/hub/.env.example and docs/hub.md say what the version is instead of naming one, so a release does not
have to edit them."""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from evo_agents import __version__
from evo_agents.cli import main

ROOT = Path(__file__).parents[2]
PLUGINS = ROOT / "plugins"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
BUMP = ROOT / "scripts" / "bump_version.py"
PIN = re.compile(r"evo-ak==([\w.+!-]+)")
RELEASE = re.compile(r"\b\d+\.\d+\.\d+\b")
SINK = "claude-code@anthropic"
# Per plugin: the MCP server's command after `evo-agents`, the hook command after `evo-agents`, and how many hooks.
EXPECTED = {
    "evo-kg": (["kg", "serve", "--sink", SINK], "kg hook ", 5),
    "evo-hub": (["hub", "mcp", "--sink", SINK], "hub hook ", 8),
}
NAMES = sorted(EXPECTED)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def hook_commands(name: str) -> list[str]:
    hooks = load(PLUGINS / name / "hooks" / "hooks.json")["hooks"]
    return [hook["command"] for groups in hooks.values() for group in groups for hook in group["hooks"]]


def test_version_is_a_plain_release():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_the_marketplace_lists_every_plugin_and_nothing_else():
    entries = load(MARKETPLACE)["plugins"]
    directories = sorted(path.name for path in PLUGINS.iterdir() if path.is_dir())
    assert sorted(entry["name"] for entry in entries) == directories == NAMES
    for entry in entries:
        assert entry["source"] == f"./plugins/{entry['name']}"
        assert load(PLUGINS / entry["name"] / ".claude-plugin" / "plugin.json")["name"] == entry["name"]
        assert "version" not in entry, entry["name"]


def mcp_server(name: str) -> list[str]:
    """What a plugin's MCP server runs after ``evo-agents``, the CLI on the PATH."""
    servers = load(PLUGINS / name / ".mcp.json")["mcpServers"]
    assert list(servers) == [name] and set(servers[name]) - {"alwaysLoad"} == {"command", "args"}
    assert servers[name]["command"] == "evo-agents"
    return servers[name]["args"]


def test_only_the_kg_tools_load_upfront():
    # Claude Code defers MCP tools behind ToolSearch; a session then spent a turn loading kg_search before its first
    # search. evo-kg's seven tools load with the session, evo-hub's many stay deferred.
    assert load(PLUGINS / "evo-kg" / ".mcp.json")["mcpServers"]["evo-kg"]["alwaysLoad"] is True
    assert "alwaysLoad" not in load(PLUGINS / "evo-hub" / ".mcp.json")["mcpServers"]["evo-hub"]


@pytest.mark.parametrize("name", NAMES)
def test_mcp_server_and_hooks_run_evo_agents_from_the_path(name):
    assert mcp_server(name) == EXPECTED[name][0]
    _, command, count = EXPECTED[name]
    commands = hook_commands(name)
    assert len(commands) == count
    for line in commands:
        # The exit status is dropped: an evo-agents that is missing, or older and refuses the subcommand with
        # argparse's 2, which would block a PreToolUse or Stop hook (tests/hub/test_hooks.py runs it).
        assert re.fullmatch(rf"evo-agents {command}[\w -]+ \|\| true", line), line


@pytest.mark.parametrize("name", NAMES)
def test_no_file_of_the_plugin_pins_a_release(name):
    for path in (PLUGINS / name).rglob("*"):
        if path.is_file() and "results" not in path.relative_to(PLUGINS / name).parts:
            assert PIN.findall(path.read_text(encoding="utf-8", errors="replace")) == [], path


def test_the_readme_pins_no_version():
    assert PIN.findall((ROOT / "README.md").read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("name", NAMES)
def test_plugin_carries_the_package_version(name):
    assert load(PLUGINS / name / ".claude-plugin" / "plugin.json")["version"] == __version__


def test_web_package_names_no_version():
    assert "version" not in load(ROOT / "web" / "package.json")


def test_hub_image_tag_examples_name_no_version():
    lines = (ROOT / "deploy" / "hub" / ".env.example").read_text(encoding="utf-8").splitlines()
    setting = next(i for i, line in enumerate(lines) if line.startswith("EVO_HUB_VERSION="))
    # The dev stack builds its own images and tags them "local"; a release number here would shadow the real image.
    assert lines[setting] == "EVO_HUB_VERSION=local"
    comment = []
    for line in reversed(lines[:setting]):
        if not line.startswith("#"):
            break
        comment.append(line)
    assert comment and RELEASE.findall(" ".join(comment)) == []
    docs = (ROOT / "docs" / "hub.md").read_text(encoding="utf-8").splitlines()
    row = next(line for line in docs if line.startswith("| `EVO_HUB_VERSION` |"))
    assert RELEASE.findall(row) == []


def copy_of_the_versioned_files(root: Path) -> Path:
    (root / "evo_agents").mkdir()
    shutil.copy(ROOT / "evo_agents" / "__init__.py", root / "evo_agents" / "__init__.py")
    shutil.copytree(PLUGINS, root / "plugins")
    return root


def bump(root: Path, version: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(BUMP), version, "--root", str(root)], capture_output=True, text=True, encoding="utf-8"
    )


def test_bump_moves_the_package_and_every_plugin_to_the_new_version(tmp_path):
    root = copy_of_the_versioned_files(tmp_path)
    result = bump(root, "98.7.6")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == f"{__version__} -> 98.7.6"
    assert '__version__ = "98.7.6"\n' in (root / "evo_agents" / "__init__.py").read_text(encoding="utf-8")
    assert [line.split(":")[0] for line in result.stdout.splitlines()[:-1]] == [
        "evo_agents/__init__.py",
        *(f"plugins/{name}/.claude-plugin/plugin.json" for name in NAMES),
    ]
    for name in NAMES:
        assert load(root / "plugins" / name / ".claude-plugin" / "plugin.json")["version"] == "98.7.6"


@pytest.mark.parametrize("version", ["0.12", "v0.12.0", "0.12.0rc1", __version__])
def test_bump_refuses_a_version_it_cannot_ship_and_changes_nothing(tmp_path, version):
    root = copy_of_the_versioned_files(tmp_path)
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    result = bump(root, version)
    assert result.returncode != 0
    assert result.stderr.strip()
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


def test_cli_reports_the_version(capsys):
    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == f"evo-agents {__version__}"
