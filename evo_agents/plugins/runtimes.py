"""The runtimes plugins contribute: agents that speak the Agent Client Protocol (ACP, https://agentclientprotocol.com).

A plugin declares a runtime in its manifest (``contributes.runtimes``, ``evo_agents.plugins.manifest``) with ``acp``,
the command of an ACP agent, the models it offers (``models``) and the environment variables its agent needs
(``secrets``). The hub registers it as ``<plugin>/<name>`` (``runtime_name``) in each project that enabled the plugin,
next to the runtimes of the core (claude-code, opencode, codex), and a run asks for it by that name. A worker whose
owner accepted the plugin gets the declaration from the hub, as an ``AcpRuntime`` (``to_json``, ``from_json``), in the
answer to its heartbeat, finds the command on its PATH, and runs a run of the runtime through the core adapter of ACP
(``evo_agents.worker.runtimes.acp``) with the declaration the claim carries.

Standard library only: the hub, the worker and the command line share this module.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from evo_agents.plugins.manifest import (
    MAX_COMMAND_ITEMS,
    MAX_RUNTIME_MODELS,
    MAX_RUNTIME_SECRETS,
    PLUGIN_ID,
    Manifest,
)

SEPARATOR = "/"  # between a plugin's id and the name of its runtime: sandbox/fake
CONTRIBUTION = r"[a-z][a-z0-9_-]{0,62}"  # a contribution's name, as plugin.schema.json has it
PLUGIN_RUNTIME = re.compile(PLUGIN_ID[1:-1] + SEPARATOR + CONTRIBUTION)
SECRET_VARIABLE = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
MAX_ARGUMENT_CHARS = 1000
MAX_MODEL_CHARS = 200
MAX_TITLE_CHARS = 200
_LINE = re.compile(r"[^\x00-\x1f\x7f]+")


def runtime_name(plugin: str, name: str) -> str:
    """The name the hub registers runtime ``name`` of plugin ``plugin`` under: ``sandbox/fake``."""
    return f"{plugin}{SEPARATOR}{name}"


def is_plugin_runtime(name: object) -> bool:
    """Whether ``name`` is the name of a plugin's runtime, ``<plugin>/<name>``, rather than one of the core's."""
    return isinstance(name, str) and PLUGIN_RUNTIME.fullmatch(name) is not None


def plugin_of(runtime: str) -> str | None:
    """The id of the plugin whose runtime ``runtime`` is; None for a runtime of the core."""
    return runtime.partition(SEPARATOR)[0] if is_plugin_runtime(runtime) else None


def _strings(value, what: str, *, bound: int, chars: int, pattern: re.Pattern = _LINE) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > bound:
        raise ValueError(f"{what} is a list of at most {bound} strings")
    for item in value:
        if not isinstance(item, str) or not 1 <= len(item) <= chars or not pattern.fullmatch(item):
            raise ValueError(f"{what} holds {item!r}, which is not one line of 1 to {chars} characters")
    return tuple(value)


@dataclass(frozen=True)
class AcpRuntime:
    """A runtime a plugin declared with ``acp``, at the version of the plugin the hub has installed."""

    runtime: str  # <plugin>/<name>, as runs name it
    plugin: str
    version: str  # the plugin's installed version
    title: str
    command: tuple[str, ...]  # the program, then its arguments
    models: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()

    @property
    def program(self) -> str:
        return self.command[0]

    def to_json(self) -> dict:
        return {
            "runtime": self.runtime,
            "plugin": self.plugin,
            "version": self.version,
            "title": self.title,
            "command": list(self.command),
            "models": list(self.models),
            "secrets": list(self.secrets),
        }

    @classmethod
    def from_json(cls, data: object) -> AcpRuntime:
        """The declaration ``to_json`` gave, as the hub sends it; ValueError when it is not one."""
        if not isinstance(data, Mapping):
            raise ValueError("a runtime's declaration is a JSON object")
        runtime, plugin, version = data.get("runtime"), data.get("plugin"), data.get("version")
        if not is_plugin_runtime(runtime) or plugin_of(runtime) != plugin:
            raise ValueError(f"{runtime!r} is not <plugin>/<name> of plugin {plugin!r}")
        if not isinstance(version, str) or not _LINE.fullmatch(version):
            raise ValueError("a runtime's declaration names the plugin's version")
        title = data.get("title") or runtime
        if not isinstance(title, str) or len(title) > MAX_TITLE_CHARS or not _LINE.fullmatch(title):
            raise ValueError("a runtime's title is one line of at most 200 characters")
        command = _strings(data.get("command"), "command", bound=MAX_COMMAND_ITEMS, chars=MAX_ARGUMENT_CHARS)
        if not command:
            raise ValueError("a runtime's command names its program")
        models = _strings(data.get("models") or [], "models", bound=MAX_RUNTIME_MODELS, chars=MAX_MODEL_CHARS)
        secrets = _strings(
            data.get("secrets") or [], "secrets", bound=MAX_RUNTIME_SECRETS, chars=64, pattern=SECRET_VARIABLE
        )
        return cls(runtime, plugin, version, title, command, models, secrets)


def acp_runtimes(manifest: Manifest) -> list[AcpRuntime]:
    """The runtimes ``manifest`` declares with ``acp``, in its order, as the hub registers them."""
    return [
        AcpRuntime(
            runtime=runtime_name(manifest.id, item["name"]),
            plugin=manifest.id,
            version=manifest.version,
            title=item.get("title") or runtime_name(manifest.id, item["name"]),
            command=tuple(item["acp"]["command"]),
            models=tuple(item.get("models") or ()),
            secrets=tuple(item.get("secrets") or ()),
        )
        for item in manifest.contributions("runtimes")
        if "acp" in item
    ]
