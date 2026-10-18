"""Core plugins: Python packages installed beside evo-agents that declare themselves in the entry point group
``evo_agents.plugins``.

An entry point of the group names a manifest, the same object ``evo-plugin.json`` holds (``evo_agents.plugins.
manifest``) as a dict, or a function that returns one, with ``tier: core``. Its contributions name their code with
``python`` (``module:attribute``), which the process that uses the extension point imports when it needs it:

- ``runtimes``: a runtime adapter of the worker, a subclass of ``evo_agents.worker.adapter.Adapter``;
- ``kg_connectors``: a connector of the knowledge graph, a function of a ``ConnectorContext``;
- ``channels``, ``routes``, ``mcp_tools``, ``jobs``, ``events``: the extension points of the hub, whose processes
  read them through ``evo_agents.hub.plugin_calls`` (``docs/plugins.md``, section Extension points); ``tables``: the
  tables of the hub's schema the plugin owns.

The registries of runtime adapters (``evo_agents.worker.adapter``) and of kg connectors (``evo_agents.kg.protocol.
runner``, ``evo_agents.harness``) read these contributions beside their own entry point groups
(``evo_agents.worker.adapters``, ``evo_agents.kg.connectors``), which keep working as they did. An entry point that
does not load, does not give a manifest, gives one that is not a core plugin's, or names ``engines`` and
``plugin_api`` this release does not run (``evo_agents.plugins.compat``) is logged and left out; a broken
distribution never stops the process that reads the group. Installing the package is what trusts it: what runs in
a process is what its operator, or the member who owns the machine, installed there.

A core plugin that contributes agent packs ships them in its package, each in ``packs/<name>/`` beside the module its
entry point names: ``image_bundle`` packs them with the manifest into a bundle, which the hub hands the workers of the
runs whose profiles attach them, as it hands those of a published plugin.

Standard library only.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path

from evo_agents.plugins import agent_packs, bundle, compat
from evo_agents.plugins.manifest import MANIFEST_FILE, Manifest, ManifestError, read_manifest

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "evo_agents.plugins"


@dataclass(frozen=True)
class CorePlugin:
    """A core plugin found in the entry points: the entry point's name and value, and its manifest."""

    entry_point: str
    value: str
    manifest: Manifest


def _manifest(point) -> Manifest:
    loaded = point.load()
    data = loaded() if callable(loaded) else loaded
    if not isinstance(data, dict):
        raise ManifestError([f"{point.value} gives {type(data).__name__}, not a manifest"])
    manifest = read_manifest(data)
    if manifest.tier != "core":
        raise ManifestError([f"{manifest.id} is not a core plugin (tier {manifest.tier}): install it on a hub instead"])
    problem = compat.problem(manifest.engines, manifest.plugin_api, contributes=manifest.contributes)
    if problem is not None:
        raise ManifestError([f"{manifest.id} {manifest.version} does not run here: {problem}"])
    return manifest


def core_plugins() -> list[CorePlugin]:
    """The core plugins installed beside evo-agents, by entry point name; those that do not load are logged."""
    try:
        points = sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda point: point.name)
    except Exception:  # a broken distribution's metadata must not stop the process
        log.warning("cannot read the entry points of the core plugins", exc_info=True)
        return []
    found = []
    for point in points:
        try:
            manifest = _manifest(point)
        except Exception as exc:  # the plugin's own module may raise anything while it loads
            log.warning(
                "core plugin not loaded",
                extra={"entry_point": point.name, "value": point.value, "error": f"{type(exc).__name__}: {exc}"},
            )
            continue
        found.append(CorePlugin(point.name, point.value, manifest))
    return found


def python_contributions(point: str, plugins: list[CorePlugin] | None = None) -> dict[str, str]:
    """name -> ``module:attribute`` of every core plugin's contributions to extension point ``point``; when two
    plugins contribute one name, the plugin whose entry point sorts last wins, and the clash is logged."""
    found: dict[str, str] = {}
    owners: dict[str, str] = {}
    for plugin in core_plugins() if plugins is None else plugins:
        for name, ref in plugin.manifest.python_refs(point).items():
            if name in found and found[name] != ref:
                log.warning(
                    "two core plugins contribute one name",
                    extra={"point": point, "contribution": name, "plugins": [owners[name], plugin.manifest.id]},
                )
            found[name], owners[name] = ref, plugin.manifest.id
    return found


def package_directory(plugin: CorePlugin) -> Path:
    """The directory of the package (or module) core plugin ``plugin``'s entry point names."""
    module = plugin.value.partition(":")[0]
    spec = importlib.util.find_spec(module)
    if spec is None:
        raise bundle.BundleError(f"core plugin {plugin.manifest.id}: no module {module}")
    if spec.submodule_search_locations:
        return Path(next(iter(spec.submodule_search_locations)))
    if spec.origin is None:
        raise bundle.BundleError(f"core plugin {plugin.manifest.id}: module {module} has no file")
    return Path(spec.origin).parent


def image_bundle(plugin: CorePlugin) -> bundle.Bundle:
    """The bundle of core plugin ``plugin``'s agent packs: its manifest as ``evo-plugin.json`` and each pack its
    manifest declares, from ``packs/<name>/`` of its package; BundleError naming what is missing or wrong."""
    root = package_directory(plugin)
    data = json.dumps(plugin.manifest.data, sort_keys=True, indent=1, ensure_ascii=False).encode()
    entries = [bundle.Entry(MANIFEST_FILE, False, data)]
    for item in plugin.manifest.contributions("agent_packs"):
        top = agent_packs.directory(item["name"])
        entries.extend(bundle.directory_entries(root / top, top))
    return bundle.bundle_of(entries, f"core plugin {plugin.manifest.id}")
