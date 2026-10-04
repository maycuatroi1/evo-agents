"""Each plugin pins the package it runs: .mcp.json, hooks.json, plugin.json and marketplace.json of evo-kg and evo-hub
must all carry ``evo_agents.__version__``, so a release tag ships plugins that start the version they were built
with. The web's package.json carries it too, since the hub-web image of a release is tagged with it, and the
example image tag named next to EVO_HUB_VERSION in deploy/hub/.env.example and docs/hub.md is the current release."""

import json
import re
from pathlib import Path

import pytest

from evo_agents import __version__
from evo_agents.cli import main

ROOT = Path(__file__).parents[2]
PLUGINS = ROOT / "plugins"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
PIN = re.compile(r"evo-ak==([\w.+!-]+)")
SINK = "claude-code@anthropic"
# Per plugin: the MCP server's command after `evo-agents`, the hook command after `evo-agents`, and how many hooks.
EXPECTED = {
    "evo-kg": (["kg", "serve", "--sink", SINK], "kg hook ", 5),
    "evo-hub": (["hub", "mcp", "--sink", SINK], "hub hook ", 2),
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


@pytest.mark.parametrize("name", NAMES)
def test_mcp_server_runs_the_package_version_through_uvx(name):
    servers = load(PLUGINS / name / ".mcp.json")["mcpServers"]
    # No --offline here: the server's first start is what downloads and caches the package.
    assert servers == {
        name: {"command": "uvx", "args": ["--from", f"evo-ak=={__version__}", "evo-agents", *EXPECTED[name][0]]}
    }


@pytest.mark.parametrize("name", NAMES)
def test_every_hook_runs_the_package_version_and_never_fails_the_session(name):
    _, command, count = EXPECTED[name]
    commands = hook_commands(name)
    assert len(commands) == count
    prefix = f"command -v uvx >/dev/null 2>&1 && uvx --offline --from evo-ak=={__version__} evo-agents {command}"
    for line in commands:
        assert line.startswith(prefix), line
        assert line.endswith(" || true"), line


@pytest.mark.parametrize("name", NAMES)
def test_no_other_version_is_pinned_in_the_plugin(name):
    for path in (PLUGINS / name / ".mcp.json", PLUGINS / name / "hooks" / "hooks.json"):
        pins = PIN.findall(path.read_text(encoding="utf-8"))
        assert pins and set(pins) == {__version__}, path
    for path in (PLUGINS / name).rglob("*"):
        if path.is_file() and "results" not in path.relative_to(PLUGINS / name).parts:
            pins = PIN.findall(path.read_text(encoding="utf-8", errors="replace"))
            assert set(pins) <= {__version__}, path


def test_no_other_version_is_pinned_in_the_readme():
    pins = PIN.findall((ROOT / "README.md").read_text(encoding="utf-8"))
    assert pins and set(pins) == {__version__}


@pytest.mark.parametrize("name", NAMES)
def test_plugin_and_marketplace_carry_the_package_version(name):
    assert load(PLUGINS / name / ".claude-plugin" / "plugin.json")["version"] == __version__
    entries = load(MARKETPLACE)["plugins"]
    assert [e["version"] for e in entries if e["name"] == name] == [__version__]


def test_web_package_carries_the_package_version():
    assert load(ROOT / "web" / "package.json")["version"] == __version__


def test_hub_image_tag_examples_name_the_package_version():
    lines = (ROOT / "deploy" / "hub" / ".env.example").read_text(encoding="utf-8").splitlines()
    setting = next(i for i, line in enumerate(lines) if line.startswith("EVO_HUB_VERSION="))
    # The dev stack builds its own images and tags them "local"; a release number here would shadow the real image.
    assert lines[setting] == "EVO_HUB_VERSION=local"
    comment = []
    for line in reversed(lines[:setting]):
        if not line.startswith("#"):
            break
        comment.append(line)
    assert re.findall(r"\b\d+\.\d+\.\d+\b", " ".join(comment)) == [__version__]
    docs = (ROOT / "docs" / "hub.md").read_text(encoding="utf-8").splitlines()
    row = next(line for line in docs if line.startswith("| `EVO_HUB_VERSION` |"))
    assert re.findall(r"\b\d+\.\d+\.\d+\b", row) == [__version__]


def test_cli_reports_the_version(capsys):
    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == f"evo-agents {__version__}"
