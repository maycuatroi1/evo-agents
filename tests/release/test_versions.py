"""The plugin pins the package it runs: .mcp.json, hooks.json, plugin.json and marketplace.json must all carry
``evo_agents.__version__``, so a release tag ships a plugin that starts the version it was built with."""

import json
import re
from pathlib import Path

import pytest

from evo_agents import __version__
from evo_agents.cli import main

ROOT = Path(__file__).parents[2]
PLUGIN = ROOT / "plugins" / "evo-kg"
PIN = re.compile(r"evo-ak==([\w.+!-]+)")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def hook_commands() -> list[str]:
    hooks = load(PLUGIN / "hooks" / "hooks.json")["hooks"]
    return [hook["command"] for groups in hooks.values() for group in groups for hook in group["hooks"]]


def test_version_is_a_plain_release():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_mcp_server_runs_the_package_version_through_uvx():
    server = load(PLUGIN / ".mcp.json")["mcpServers"]["evo-kg"]
    # No --offline here: the server's first start is what downloads and caches the package.
    assert server == {
        "command": "uvx",
        "args": ["--from", f"evo-ak=={__version__}", "evo-agents", "kg", "serve", "--sink", "claude-code@anthropic"],
    }


def test_every_hook_runs_the_package_version_and_never_fails_the_session():
    commands = hook_commands()
    assert len(commands) == 5
    prefix = f"command -v uvx >/dev/null 2>&1 && uvx --offline --from evo-ak=={__version__} evo-agents kg hook "
    for command in commands:
        assert command.startswith(prefix), command
        assert command.endswith(" || true"), command


def test_no_other_version_is_pinned_in_the_plugin_or_readme():
    for path in (PLUGIN / ".mcp.json", PLUGIN / "hooks" / "hooks.json", ROOT / "README.md"):
        pins = PIN.findall(path.read_text(encoding="utf-8"))
        assert pins and set(pins) == {__version__}, path


def test_plugin_and_marketplace_carry_the_package_version():
    assert load(PLUGIN / ".claude-plugin" / "plugin.json")["version"] == __version__
    entries = load(ROOT / ".claude-plugin" / "marketplace.json")["plugins"]
    assert [e["version"] for e in entries if e["name"] == "evo-kg"] == [__version__]


def test_cli_reports_the_version(capsys):
    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == f"evo-agents {__version__}"
