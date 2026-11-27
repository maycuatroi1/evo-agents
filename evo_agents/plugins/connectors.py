"""The one registry of connector names: the builtins, the ``kg_connectors`` of core plugins, the entry points of group
``evo_agents.kg.connectors`` and ``python:module:function``.

It reads the builtins by the ``module:attribute`` of their class (in ``evo_agents.kg.connectors``) and imports one
only when a run loads it. It sits in ``evo_agents.plugins``, below both the graph and ``evo_agents.harness``, so the
harness validates a knowledge.yaml against these names without importing the graph, whose connectors import the
harness.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from importlib import metadata

from evo_agents.plugins.core import python_contributions

CONNECTOR_GROUP = "evo_agents.kg.connectors"
PYTHON = "python:"


class ConnectorError(RuntimeError):
    """The connector could not be started: unknown name, missing credential, bad command."""


@dataclass(frozen=True)
class Builtin:
    name: str
    target: str  # module:attribute of its Connector class
    directory: bool = False  # it reads a directory on this machine, whose files the build maps to directory nodes


BUILTINS = (
    Builtin("harness", "evo_agents.kg.connectors.harness:HarnessConnector", directory=True),
    Builtin("git", "evo_agents.kg.connectors.git:GitConnector", directory=True),
    Builtin("exec", "evo_agents.kg.connectors.command:CommandConnector"),
)


def _imported(target: str, default: str | None = None):
    module_name, _, attr = target.partition(":")
    return getattr(importlib.import_module(module_name), attr or default or "")


class ConnectorRegistry:
    def __init__(self, builtins: tuple[Builtin, ...]):
        self.builtins = {b.name: b for b in builtins}

    def names(self) -> set[str]:
        """The builtin connectors, those core plugins contribute (``evo_agents.plugins.core``) and the entry points of
        group evo_agents.kg.connectors."""
        names = set(self.builtins)
        names.update(python_contributions("kg_connectors"))
        try:
            names.update(ep.name for ep in metadata.entry_points(group=CONNECTOR_GROUP))
        except Exception:  # pragma: no cover - broken environments still validate builtins
            pass
        return names

    def directory(self, name: str | None) -> bool:
        """Whether the connector ``name`` reads a directory, so its files and their directories become nodes."""
        found = self.builtins.get(name or "")
        return found is not None and found.directory

    def load(self, name: str):
        """What runs the connector ``name``: a Connector class for a builtin, else what its contribution, the
        ``python:module:function`` or its entry point names (a function of a ConnectorContext, or a Connector class)."""
        found = self.builtins.get(name)
        if found is not None:
            return _imported(found.target)
        contributed = python_contributions("kg_connectors").get(name)
        if contributed is not None:
            try:
                return _imported(contributed)
            except (ImportError, AttributeError) as exc:
                raise ConnectorError(f"cannot load connector {name!r} ({contributed}): {exc}") from exc
        if name.startswith(PYTHON):
            try:
                return _imported(name[len(PYTHON) :], "run")
            except (ImportError, AttributeError) as exc:
                raise ConnectorError(f"cannot load connector {name!r}: {exc}") from exc
        for ep in metadata.entry_points(group=CONNECTOR_GROUP):
            if ep.name == name:
                return ep.load()
        raise ConnectorError(f"unknown connector {name!r}")


REGISTRY = ConnectorRegistry(BUILTINS)
