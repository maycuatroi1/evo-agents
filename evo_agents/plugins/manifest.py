"""The manifest of a plugin, ``evo-plugin.json`` at the top of its bundle, read against
``evo_agents/harness/schemas/plugin.schema.json`` (the seam plugin-v1).

``read_manifest`` takes the manifest as JSON bytes or as an object already parsed and gives a ``Manifest``, or raises
ManifestError listing every problem: the schema's, then what a schema cannot say. Contributions to one extension
point have distinct names; a contribution's ``python`` (``module:attribute`` of the code that implements it, run in
the hub's or the worker's process) belongs to a core plugin only; the ``engines`` range must read
(``evo_agents.plugins.compat``); the config's ``required`` names its own properties.

The schema file is read where the package installs it, next to the harness schemas, without importing
``evo_agents.harness``. Standard library only, besides ``evo_agents.schema``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from evo_agents.plugins.compat import SpecError, parse_spec
from evo_agents.schema import errors, validate

MANIFEST_FILE = "evo-plugin.json"
SCHEMA_FILE = Path(__file__).resolve().parent.parent / "harness" / "schemas" / "plugin.schema.json"
MAX_MANIFEST = 256 * 1024  # bytes of evo-plugin.json
TIERS = ("isolated", "core")
EXTENSION_POINTS = (
    "channels",
    "routes",
    "mcp_tools",
    "jobs",
    "events",
    "pages",
    "agent_packs",
    "agent_types",
    "runtimes",
    "environments",
    "kg_connectors",
)
PERMISSIONS = (
    "runs:read",
    "runs:dispatch",
    "chats:read",
    "chats:write",
    "plans:read",
    "memories:read",
    "notifications:send",
)


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
    data: dict = field(repr=False, compare=False)

    def contributions(self, point: str) -> tuple[dict, ...]:
        """What the plugin contributes to extension point ``point``, in the manifest's order."""
        return self.contributes.get(point, ())

    def python_refs(self, point: str) -> dict[str, str]:
        """name -> ``module:attribute`` of the contributions to ``point`` that name their Python."""
        return {item["name"]: item["python"] for item in self.contributions(point) if "python" in item}


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
            if "python" in item and not core:
                found.append(
                    f"contributes.{point}[{index}].python: only a core plugin runs Python in the hub's or the "
                    "worker's process; an isolated plugin implements it in its own process"
                )
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
        data=data,
    )
