"""The manifest of a plugin, ``evo-plugin.json`` at the top of its bundle, read against
``evo_agents/harness/schemas/plugin.schema.json`` (the seam plugin-v1).

``read_manifest`` takes the manifest as JSON bytes or as an object already parsed and gives a ``Manifest``, or raises
ManifestError listing every problem: the schema's, then what a schema cannot say. Contributions to one extension
point have distinct names; a contribution's ``python`` (``module:attribute`` of the code that implements it, run in
the hub's or the worker's process) belongs to a core plugin only, and so does a ``backend`` to an isolated one; the
paths a backend names stay inside the bundle; the ``engines`` range must read (``evo_agents.plugins.compat``); the
config's ``required`` names its own properties.

A core plugin implements its contributions to the points of the hub (channels, routes, MCP tools, jobs, events) in
Python, which each names with ``python`` (one that names none does nothing there); a channel of a core plugin is a kind
of members' channel, named as the hub names those (``CHANNEL_KIND``). Only a core plugin owns tables (``tables``), and
each one's name starts with the plugin's table prefix (``table_prefix``): its id with ``_`` for ``-``, then ``_``. The
tables come from the hub's own chain of migrations, never from the plugin at run time.

A page (``pages``) of an isolated plugin names its HTML file in the bundle under ``pages/`` (``entry``), which the web
shows in a sandboxed iframe; a core plugin's page is native, a page of the hub's web itself, and names no entry.

The schema file is read where the package installs it, next to the harness schemas, without importing
``evo_agents.harness``. Standard library only, besides ``evo_agents.schema``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from evo_agents.plugins.compat import POINT_VERSIONS, SpecError, parse_spec
from evo_agents.plugins.protocol import BACKEND_POINTS
from evo_agents.schema import errors, validate

MANIFEST_FILE = "evo-plugin.json"
SCHEMA_FILE = Path(__file__).resolve().parent.parent / "harness" / "schemas" / "plugin.schema.json"
MAX_MANIFEST = 256 * 1024  # bytes of evo-plugin.json
PLUGIN_ID = r"^[a-z][a-z0-9-]{0,62}$"  # a manifest's id, as plugin.schema.json has it
TIERS = ("isolated", "core")
EXTENSION_POINTS = tuple(POINT_VERSIONS)  # each with the versions of its contract this release serves
CORE_ONLY_POINTS = ("tables",)  # what only a core plugin contributes
CHANNEL_KIND = r"^[a-z][a-z0-9_-]{0,31}$"  # a kind of members' channel (notification_channels.kind), never "web"
PERMISSIONS = (
    "runs:read",
    "runs:dispatch",
    "chats:read",
    "chats:write",
    "plans:read",
    "memories:read",
    "notifications:send",
)
# What each permission lets a plugin do on a project that enables it, as an admin of the project reads it before
# granting it.
PERMISSION_TEXT = {
    "runs:read": "read the project's runs, their events and their outcomes",
    "runs:dispatch": "dispatch runs in the project",
    "chats:read": "read the project's group chats",
    "chats:write": "post messages in the project's group chats",
    "plans:read": "read the project's plans",
    "memories:read": "read the project's memories",
    "notifications:send": "send notifications to the project's members",
}


def table_prefix(plugin_id: str) -> str:
    """What the name of every table plugin ``plugin_id`` owns starts with: ``telegram_`` for telegram, ``srs_docs_``
    for srs-docs."""
    return plugin_id.replace("-", "_") + "_"


class ManifestError(ValueError):
    """A manifest that is not one: ``problems`` says each thing that is wrong, with where."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@cache
def schema() -> dict:
    return json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Manifest:
    """A manifest that passed every check. ``data`` is the object as it was read."""

    id: str
    version: str
    publisher: str
    tier: str
    engines: dict
    plugin_api: int
    name: str
    description: str
    permissions: tuple[str, ...]
    config: dict | None
    contributes: dict[str, tuple[dict, ...]]
    backend: dict | None
    data: dict = field(repr=False, compare=False)

    def contributions(self, point: str) -> tuple[dict, ...]:
        """What the plugin contributes to extension point ``point``, in the manifest's order."""
        return self.contributes.get(point, ())

    def serves(self) -> tuple[str, ...]:
        """The extension points an isolated plugin serves from its backend, in BACKEND_POINTS' order."""
        return tuple(point for point in BACKEND_POINTS if self.contributions(point)) if self.tier == "isolated" else ()

    def python_refs(self, point: str) -> dict[str, str]:
        """name -> ``module:attribute`` of the contributions to ``point`` that name their Python."""
        return {item["name"]: item["python"] for item in self.contributions(point) if "python" in item}

    def contribution(self, point: str, name: str) -> dict | None:
        """The contribution named ``name`` to extension point ``point``, or None."""
        return next((item for item in self.contributions(point) if item["name"] == name), None)

    def pages(self) -> tuple[dict, ...]:
        """The pages the plugin contributes to the hub's web, in the manifest's order."""
        return self.contributions("pages")

    def tables(self) -> tuple[str, ...]:
        """The tables a core plugin owns, which the hub's migrations create."""
        return tuple(item["name"] for item in self.contributions("tables"))

    def per_project(self) -> bool:
        """Whether a project enables the plugin for anything: it asks for a permission, takes a config, or contributes
        an MCP tool, a job, an event, a page or a route under /v1/x/<id>/<project>. A core plugin that does none of that
        (Telegram: a channel of members and routes of its own) runs wherever it is installed, and the hub's store
        does not list it."""
        if self.tier == "isolated" or self.permissions or self.config:
            return True
        if any(self.contributions(point) for point in ("mcp_tools", "jobs", "events", "pages")):
            return True
        return any("prefix" not in item for item in self.contributions("routes"))


def _semantics(data: dict) -> list[str]:
    """What the schema cannot check: unique names per point, Python in core plugins only, a range that reads, and a
    config whose required properties exist."""
    found = []
    core = data.get("tier", "isolated") == "core"
    for point, items in (data.get("contributes") or {}).items():
        names = [item["name"] for item in items]
        for name in sorted({name for name in names if names.count(name) > 1}):
            found.append(f"contributes.{point}: {name!r} is contributed twice")
        for index, item in enumerate(items):
            found.extend(_contribution_problems(data["id"], point, index, item, core))
    found.extend(_backend_problems(data, core))
    try:
        parse_spec(data["engines"]["evo-agents"])
    except SpecError as exc:
        found.append(f"engines.evo-agents: {exc}")
    config = data.get("config") or {}
    declared = set(config.get("properties") or {})
    for name in config.get("required") or []:
        if name not in declared:
            found.append(f"config.required: {name!r} is not one of the config's properties")
    permissions = data.get("permissions") or []
    for permission in sorted({p for p in permissions if permissions.count(p) > 1}):
        found.append(f"permissions: {permission!r} is listed twice")
    return found


def _contribution_problems(plugin_id: str, point: str, index: int, item: dict, core: bool) -> list[str]:
    """What is wrong with contribution ``item`` to ``point`` of a core (``core``) or an isolated plugin."""
    where = f"contributes.{point}[{index}]"
    if "python" in item and not core:
        return [
            f"{where}.python: only a core plugin runs Python in the hub's or the worker's process; an isolated "
            "plugin implements it in its own process"
        ]
    if point in CORE_ONLY_POINTS and not core:
        return [f"{where}: only a core plugin owns tables, which the hub's migrations create"]
    if "prefix" in item and not (core and point == "routes"):
        return [f"{where}.prefix: only a core plugin's route names the path its router is included under"]
    if point == "tables" and not item["name"].startswith(table_prefix(plugin_id)):
        return [f"{where}: table {item['name']!r} must start with {table_prefix(plugin_id)}, the plugin's prefix"]
    if core and point == "channels" and (not re.fullmatch(CHANNEL_KIND, item["name"]) or item["name"] == "web"):
        return [f"{where}: a core plugin's channel is a kind of members' channel: {CHANNEL_KIND}, not web"]
    if point == "pages":
        return _page_problems(where, item, core)
    return []


def _page_problems(where: str, item: dict, core: bool) -> list[str]:
    """What is wrong with page ``item``: a core plugin's page is native and names no entry; an isolated plugin's
    names its HTML file, inside the bundle's pages/."""
    entry = item.get("entry")
    if core:
        if entry is None:
            return []
        return [
            f"{where}.entry: a core plugin's page is native, a page of the hub's web; only an isolated plugin's names "
            "an entry"
        ]
    if entry is None:
        return [f"{where}: an isolated plugin's page names its entry, an HTML file of its bundle under pages/"]
    if any(part in ("", ".", "..") for part in entry.split("/")):
        return [f"{where}.entry: {entry!r} must be a path inside the bundle's pages/, without empty, . or .. parts"]
    return []


def _backend_problems(data: dict, core: bool) -> list[str]:
    backend = data.get("backend")
    if backend is None:
        return []
    if core:
        return [
            "backend: a core plugin runs Python in the hub's and the worker's processes; only an isolated one "
            "has a backend"
        ]
    found = []
    for key in ("entry", "requirements"):
        path = backend.get(key)
        if path is not None and any(part in ("", ".", "..") for part in path.split("/")):
            found.append(f"backend.{key}: {path!r} must be a path inside the bundle, without empty, . or .. parts")
    return found


def _parsed(source: bytes | str | dict) -> dict:
    if isinstance(source, dict):
        return source
    if len(source) > MAX_MANIFEST:
        raise ManifestError([f"{MANIFEST_FILE} is over {MAX_MANIFEST // 1024} KiB"])
    try:
        data = json.loads(source)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError([f"{MANIFEST_FILE} is not JSON: {exc}"]) from None
    if not isinstance(data, dict):
        raise ManifestError([f"{MANIFEST_FILE} holds {type(data).__name__}, not an object"])
    return data


def read_manifest(source: bytes | str | dict) -> Manifest:
    """The manifest ``source`` holds, checked whole; ManifestError listing every problem otherwise."""
    data = _parsed(source)
    problems = [f"{issue.path or '<root>'}: {issue.message}" for issue in errors(validate(data, schema()))]
    if problems:
        raise ManifestError(problems)
    problems = _semantics(data)
    if problems:
        raise ManifestError(problems)
    contributes = {point: tuple(items) for point, items in (data.get("contributes") or {}).items()}
    return Manifest(
        id=data["id"],
        version=data["version"],
        publisher=data["publisher"],
        tier=data.get("tier", "isolated"),
        engines=dict(data["engines"]),
        plugin_api=data["plugin_api"],
        name=data.get("name") or data["id"],
        description=data.get("description", ""),
        permissions=tuple(data.get("permissions") or ()),
        config=data.get("config"),
        contributes=contributes,
        backend=data.get("backend"),
        data=data,
    )
