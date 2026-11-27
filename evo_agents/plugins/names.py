"""The names of what a plugin contributes and the hub registers as its own, ``<plugin>/<name>``, and the declarations
of those a worker runs a run with.

A runtime (``evo_agents.plugins.runtimes``), an environment (``evo_agents.plugins.run_environments``) and an agent pack
(``evo_agents.plugins.agent_packs``) of a plugin are registered under a ``PluginName``: the plugin's id, ``/``, then the
contribution's name, so no two plugins share one and none is taken for one of the core (``claude-code``, ``host``). A
runtime and an environment reach a worker as a ``PluginDeclaration``, with the plugin's id, its installed version and
a title, and each subclass adds what it declares: ``to_json`` and ``from_json`` carry it, and ``declared`` reads the
contributions of a manifest.

Standard library only: the hub, the worker and the command line share this module.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Self

from evo_agents.plugins.manifest import PLUGIN_ID, Manifest

SEPARATOR = "/"  # between a plugin's id and the name of its contribution: sandbox/fake
CONTRIBUTION = r"[a-z][a-z0-9_-]{0,62}"  # a contribution's name, as plugin.schema.json has it
PLUGIN_NAME = re.compile(PLUGIN_ID[1:-1] + SEPARATOR + CONTRIBUTION)
LINE = re.compile(r"[^\x00-\x1f\x7f]+")  # one line of text
MAX_TITLE_CHARS = 200


@dataclass(frozen=True)
class PluginName:
    """``<plugin>/<name>``: contribution ``name`` of plugin ``plugin``, as the hub registers it."""

    plugin: str
    name: str

    def __str__(self) -> str:
        return f"{self.plugin}{SEPARATOR}{self.name}"

    @classmethod
    def of(cls, plugin: str, name: str) -> str:
        """The name the hub registers contribution ``name`` of plugin ``plugin`` under: ``sandbox/fake``."""
        return str(cls(plugin, name))

    @staticmethod
    def matches(value: object) -> bool:
        """Whether ``value`` reads as ``<plugin>/<name>``."""
        return isinstance(value, str) and PLUGIN_NAME.fullmatch(value) is not None

    @classmethod
    def parse(cls, value: object) -> PluginName | None:
        """The plugin name ``value`` reads as; None when it is not one."""
        if not cls.matches(value):
            return None
        assert isinstance(value, str)
        plugin, _, name = value.partition(SEPARATOR)
        return cls(plugin, name)

    @classmethod
    def plugin_of(cls, value: object) -> str | None:
        """The id of the plugin ``value`` names a contribution of; None when it is not ``<plugin>/<name>``."""
        found = cls.parse(value)
        return None if found is None else found.plugin


class PluginDeclaration:
    """What a plugin declared of contribution point ``POINT`` and the hub hands a worker, at the version of the plugin
    it has installed. A subclass is a frozen dataclass whose first four fields are the ``PluginName`` it is registered
    under (named ``KEY``), ``plugin``, ``version`` and ``title``; it gives the rest of its JSON (``own_json``,
    ``head_problem``, ``own_from_json``), of a contribution of a manifest (``own_of``), and which contributions it
    reads (``declares``)."""

    KEY: ClassVar[str]  # runtime: the key of its name in its JSON, and the field that holds it
    WHAT: ClassVar[str]  # a runtime's: how its messages name it
    POINT: ClassVar[str]  # runtimes: the extension point of the manifest it is contributed to

    plugin: str
    version: str  # the plugin's installed version
    title: str

    @property
    def registered(self) -> str:
        """``<plugin>/<name>``, as runs name it."""
        return getattr(self, self.KEY)

    def own_json(self) -> dict:
        return {}

    def to_json(self) -> dict:
        found = {self.KEY: self.registered, "plugin": self.plugin, "version": self.version, "title": self.title}
        return found | self.own_json()

    @classmethod
    def head_problem(cls, data: Mapping) -> str | None:
        """What is wrong with ``data`` that the hub checks before its title; None when nothing is."""
        return None

    @classmethod
    def own_from_json(cls, data: Mapping) -> tuple:
        """The fields after the title of the declaration ``data`` holds; ValueError when they are not."""
        return ()

    @classmethod
    def from_json(cls, data: object) -> Self:
        """The declaration ``to_json`` gave, as the hub sends it; ValueError when it is not one."""
        if not isinstance(data, Mapping):
            raise ValueError(f"{cls.WHAT} declaration is a JSON object")
        registered, plugin, version = data.get(cls.KEY), data.get("plugin"), data.get("version")
        if PluginName.plugin_of(registered) is None or PluginName.plugin_of(registered) != plugin:
            raise ValueError(f"{registered!r} is not <plugin>/<name> of plugin {plugin!r}")
        if not isinstance(version, str) or not LINE.fullmatch(version):
            raise ValueError(f"{cls.WHAT} declaration names the plugin's version")
        problem = cls.head_problem(data)
        if problem:
            raise ValueError(problem)
        title = data.get("title") or registered
        if not isinstance(title, str) or len(title) > MAX_TITLE_CHARS or not LINE.fullmatch(title):
            raise ValueError(f"{cls.WHAT} title is one line of at most {MAX_TITLE_CHARS} characters")
        return cls(registered, plugin, version, title, *cls.own_from_json(data))  # type: ignore[call-arg]

    @classmethod
    def declares(cls, item: dict) -> bool:
        """Whether contribution ``item`` is one of this class."""
        return True

    @classmethod
    def own_of(cls, item: dict) -> tuple:
        """The fields after the title of contribution ``item``."""
        return ()

    @classmethod
    def declared(cls, manifest: Manifest) -> list[Any]:
        """What ``manifest`` declares of the class, in its order, as the hub registers it."""
        found = []
        for item in manifest.contributions(cls.POINT):
            if cls.declares(item):
                registered = PluginName.of(manifest.id, item["name"])
                title = item.get("title") or registered
                own = cls.own_of(item)
                found.append(cls(registered, manifest.id, manifest.version, title, *own))  # type: ignore[call-arg]
        return found
