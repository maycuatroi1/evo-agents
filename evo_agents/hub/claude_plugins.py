"""Whether the Claude Code session of a plugin's MCP server also has another plugin: what ``evo-agents hub mcp`` asks
before it lists the kg_* tools, which the evo-kg plugin serves in a session that has both (``mcp_proxy``).

Claude Code starts the MCP server of a plugin with CLAUDE_PLUGIN_ROOT set to that plugin's directory, and
CLAUDE_PROJECT_DIR to the session's project. Which plugins the session has comes from ``enabledPlugins`` in its
settings, keyed ``<plugin>@<marketplace>``: the user's (``settings.json`` of ``$CLAUDE_CONFIG_DIR``, else
``~/.claude``), then the project's ``.claude/settings.json`` and ``.claude/settings.local.json``, a later file
overriding a key an earlier one set. A plugin counts when one of its keys is on and ``plugins/installed_plugins.json``
of the same configuration directory has that key.

The answer leans to no: a process that is no server of a plugin Claude Code installed (CLAUDE_PLUGIN_ROOT unset or
outside the ``plugins`` directory of Claude Code's configuration, as under Codex, opencode, ``--plugin-dir`` or an MCP
server declared by hand), a settings file that does not parse, a plugin that is on but not installed. A session then
sees the kg_* tools twice rather than not at all. Settings this does not read (managed settings, ``--settings``)
count for nothing. Standard library only.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

PLUGIN_ROOT = "CLAUDE_PLUGIN_ROOT"
SETTINGS = ("settings.json", "settings.local.json")  # under the project's .claude, in the order they override


def config_dir(env: Mapping[str, str]) -> Path:
    """Where Claude Code keeps its settings and plugins: CLAUDE_CONFIG_DIR, else ``~/.claude``."""
    value = env.get("CLAUDE_CONFIG_DIR")
    return Path(value).expanduser() if value else Path.home() / ".claude"


def _installed_plugin(root: str | None, env: Mapping[str, str]) -> bool:
    """Whether ``root`` is the directory of a plugin under Claude Code's ``plugins`` directory."""
    if not root:
        return False
    try:
        return Path(root).expanduser().resolve().is_relative_to((config_dir(env) / "plugins").resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def _object(path: Path) -> dict:
    """The JSON object in ``path``; {} when it is missing, unreadable or no object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def enabled_plugins(env: Mapping[str, str], cwd: Path | None = None) -> dict:
    """``enabledPlugins`` of the user's settings, overridden key by key by the project's, then by the local ones."""
    project = Path(env.get("CLAUDE_PROJECT_DIR") or cwd or Path.cwd())
    merged: dict = {}
    for path in (config_dir(env) / "settings.json", *(project / ".claude" / name for name in SETTINGS)):
        found = _object(path).get("enabledPlugins")
        if isinstance(found, dict):
            merged.update(found)
    return merged


def session_has(plugin: str, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> bool:
    """Whether the Claude Code session whose plugin started this process has ``plugin`` (its name, as in its
    plugin.json) on and installed; False outside a plugin's server (see the module)."""
    env = os.environ if env is None else env
    if not _installed_plugin(env.get(PLUGIN_ROOT), env):
        return False
    return plugin_on(plugin, env, cwd)


def plugin_on(plugin: str, env: Mapping[str, str], cwd: Path | None = None) -> bool:
    """Whether a Claude Code session started with ``env`` in ``cwd`` would have ``plugin`` on and installed, as its
    settings and ``plugins/installed_plugins.json`` say (see the module); a worker asks it before it gives a run's
    agent the hub's MCP server itself."""
    keys = [key for key, on in enabled_plugins(env, cwd).items() if on is True and key.startswith(f"{plugin}@")]
    if not keys:
        return False
    installed = _object(config_dir(env) / "plugins" / "installed_plugins.json").get("plugins")
    return isinstance(installed, dict) and any(installed.get(key) for key in keys)
