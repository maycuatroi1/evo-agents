"""The commands plugins add to the CLI (extension point ``cli``), as a list: the one the hub gives at
``GET /v1/cli/plugins`` (``evo_agents.hub.server.plugin_command_list``), kept in ``cli-plugins.json`` of the login directory
(``~/.evo/hub``), and the one of the core plugins installed with the CLI, read from their ``cli.json`` without
importing them. ``evo_agents.hub.cli_plugin_commands`` builds the parsers of ``evo-agents hub <plugin> <command>``
from it, and ``evo_agents.hub.contract`` builds the contract under ``BUNDLED_ONLY``, from the bundled plugins alone.
Standard library only, like the rest of the client.
"""

from __future__ import annotations

import contextvars
import importlib.util
import json
from importlib.metadata import entry_points
from pathlib import Path

from evo_agents.hub.client import Hub, HubError, hub_dir, load_credentials

PATH = "/v1/cli/plugins"
CACHE_FILE = "cli-plugins.json"
VERSION = 1
ENTRY_POINT_GROUP = "evo_agents.plugins"  # evo_agents.plugins.core
BUNDLED_FILE = "cli.json"


# The list the hub gives


def cache_path() -> Path:
    return hub_dir() / CACHE_FILE


def cached() -> dict:
    """The list as the hub last gave it, ``{"plugins": [...]}``; empty when none was kept or it does not read."""
    try:
        found = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, HubError):
        return {"plugins": []}
    if not isinstance(found, dict) or not isinstance(found.get("plugins"), list):
        return {"plugins": []}
    return found


def refresh() -> dict:
    """Ask the hub for its list and keep it; the one kept when the hub cannot be asked (not signed in, unreachable,
    an older hub without the list), so the CLI works as before."""
    held = cached()
    try:
        credentials = load_credentials()
        body = Hub(credentials.url, credentials.token).call("GET", PATH)
    except (HubError, OSError):
        return held
    if not isinstance(body, dict) or body.get("version") != VERSION or not isinstance(body.get("plugins"), list):
        return held
    found = {"plugins": body["plugins"]}
    try:
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return found


def wants_refresh(argv: list[str], known: set[str], plugins: list[dict]) -> bool:
    """Whether ``argv`` (of ``evo-agents``) names a ``hub`` command the CLI has no parser for: a group that is neither
    its own nor a plugin's it knows, or a command of a plugin it knows that the list kept lacks."""
    if len(argv) < 2 or argv[0] != "hub" or argv[1].startswith("-"):
        return False
    name = argv[1]
    plugin = next((item for item in plugins if item.get("id") == name), None)
    if plugin is None:
        return name not in known
    if len(argv) < 3 or argv[2].startswith("-"):
        return False
    return argv[2] not in {command.get("name") for command in plugin.get("commands") or []}


def bundled() -> list[dict]:
    """The commands of the core plugins installed with the CLI, ``[{"id", "version", "commands"}]`` by id: each entry
    point of ``evo_agents.plugins`` whose package holds ``cli.json``, found with ``find_spec`` and read as JSON, so no
    plugin is imported. A file that does not read is left out."""
    found = []
    for point in sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda point: point.name):
        try:
            spec = importlib.util.find_spec(point.module)
        except (ImportError, ValueError):
            continue
        if spec is None or not spec.submodule_search_locations:
            continue
        for folder in spec.submodule_search_locations:
            try:
                commands = json.loads((Path(folder) / BUNDLED_FILE).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(commands, list) and commands:
                version = point.dist.version if point.dist is not None else ""
                found.append({"id": point.name, "version": version, "commands": commands, "bundled": True})
            break
    return found


# True while the contract (``evo_agents.hub.contract``) builds the parser: the bundled plugins alone, so the contract
# does not depend on the list a machine kept from its hub.
BUNDLED_ONLY: contextvars.ContextVar[bool] = contextvars.ContextVar("bundled_only", default=False)


def known_plugins() -> list[dict]:
    """The bundled plugins, then those of the list kept that no bundled one has the id of (none under BUNDLED_ONLY)."""
    mine = bundled()
    if BUNDLED_ONLY.get():
        return mine
    ids = {plugin["id"] for plugin in mine}
    return mine + [plugin for plugin in cached()["plugins"] if plugin.get("id") not in ids]
