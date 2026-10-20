"""Core plugins from outside evo-agents, as distributions: the tests of ``evo_agents.plugins.core_install`` install them
in a directory on the path (``write_dist``), and the test of the hub's image installs one from a wheel
(``plugin_wheel``). Each is one module with a manifest of tier core and an entry point of the group
``evo_agents.plugins``; the names are fictional.
"""

from __future__ import annotations

from pathlib import Path

from evo_agents.plugins.core import ENTRY_POINT_GROUP
from tests.plugins.backends import make_wheel

# A core plugin that loads on this release; MARKER lets a test find its file in an image's layers.
GOOD = '''"""A core plugin of tests.plugins.core_fakes: {marker}"""

MANIFEST = {{
    "id": "{plugin}",
    "version": "{version}",
    "publisher": "lumen",
    "tier": "core",
    "engines": {{"evo-agents": ">=0.16"}},
    "plugin_api": 1,
    "contributes": {{"kg_connectors": [{{"name": "{plugin}", "python": "{module}:records"}}]}},
}}


def records(context):
    return []
'''

# A core plugin written for another core plugin API than the hub serves: its manifest function calls require() with a
# major number past the hub's. A hub without the API module fails the import, as a hub older than the API would.
WRONG_API = '''"""A core plugin of tests.plugins.core_fakes that needs another core plugin API."""


def manifest():
    from evo_agents.hub.server.core_plugin_api import API_VERSION, require

    require(API_VERSION[0] + 1, 0)
    manifest = {{"id": "{plugin}", "version": "{version}", "publisher": "lumen", "tier": "core", "plugin_api": 1}}
    return {{**manifest, "engines": {{"evo-agents": ">=0.16"}}}}
'''

# A manifest that is no core plugin's (tier isolated), one whose engines no release reaches, one that is no dict, and a
# module that fails as it loads.
ISOLATED = """MANIFEST = {{"id": "{plugin}", "version": "{version}", "publisher": "lumen", "tier": "isolated",
            "engines": {{"evo-agents": ">=0.16"}}, "plugin_api": 1}}
"""
TOO_NEW = """MANIFEST = {{"id": "{plugin}", "version": "{version}", "publisher": "lumen", "tier": "core",
            "engines": {{"evo-agents": ">=99"}}, "plugin_api": 1}}
"""
NOT_A_MANIFEST = """MANIFEST = ["{plugin}"]
"""
BROKEN_IMPORT = """import lumen_dependency_nobody_installed  # noqa: F401

MANIFEST = {{}}
"""


def source(template: str, plugin: str, module: str, version: str = "1.0.0", marker: str = "") -> str:
    return template.format(plugin=plugin, module=module, version=version, marker=marker)


def _attribute(template: str) -> str:
    return "manifest" if template is WRONG_API else "MANIFEST"


def entry_points_txt(entry_point: str, module: str, attribute: str) -> str:
    return f"[{ENTRY_POINT_GROUP}]\n{entry_point} = {module}:{attribute}\n"


def write_dist(
    directory: Path,
    name: str,
    template: str,
    *,
    module: str,
    entry_point: str | None = None,
    plugin: str | None = None,
    version: str = "1.0.0",
) -> None:
    """Install distribution ``name`` ``version`` in ``directory``, as pip would in site-packages: module ``module``
    from ``template`` and an entry point of the group (``entry_point``, default ``name``) naming its manifest."""
    (directory / f"{module}.py").write_text(source(template, plugin or name, module, version))
    info = directory / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    (info / "entry_points.txt").write_text(entry_points_txt(entry_point or name, module, _attribute(template)))


def plugin_wheel(
    directory: Path, name: str, version: str, module: str, marker: str = "", template: str = GOOD
) -> tuple[Path, str]:
    """A wheel of distribution ``name`` holding the core plugin ``template`` (GOOD by default) as module ``module``,
    written into ``directory``; its path and SHA-256."""
    dist = f"{name.replace('-', '_')}-{version}"
    files = {
        f"{module}.py": source(template, name, module, version, marker),
        f"{dist}.dist-info/entry_points.txt": entry_points_txt(name, module, _attribute(template)),
    }
    return make_wheel(directory, name, version, files)
