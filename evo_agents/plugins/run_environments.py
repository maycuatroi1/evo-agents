"""The run environments plugins contribute: container images a worker runs the agent of a run in.

A plugin declares an environment in its manifest (``contributes.environments``, ``evo_agents.plugins.manifest``): an
image pinned by its digest, the network the container reaches (``none`` or ``outbound``) and its mounts, tmpfs or
named volumes (``Mount``). The hub registers it as ``<plugin>/<name>`` (``environment_name``) in each project that
enabled the plugin. The eighth axis of an agent profile, ``environment`` (``evo_agents.hub.profiles``), names
environments: ``host``, the worker's own machine as runs always had it (``HOST``), and those of plugins. A claim picks
the first environment of the profile's allowlist the claiming worker takes and hands its declaration, as a
``ContainerEnvironment`` (``to_json``, ``from_json``), to the worker, whose container provider runs the agent there
(``evo_agents.worker.environments``). A profile whose axis allows all runs on the host.

Standard library only: the hub, the worker and the command line share this module.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from evo_agents.plugins.manifest import FORBIDDEN_TARGETS, MAX_ENVIRONMENT_MOUNTS, Manifest
from evo_agents.plugins.runtimes import PLUGIN_RUNTIME, SEPARATOR

HOST = "host"  # the worker's own machine: no container, as every run ran before environments
PROVIDERS = ("host", "container")  # the providers of the core; an environment of a plugin is a container
NETWORKS = ("none", "outbound")
MOUNT_TYPES = ("tmpfs", "volume")
MAX_IMAGE_CHARS = 512
MAX_SIZE_MB = 65536
MAX_TITLE_CHARS = 200
# [registry[:port]/]repository[:tag]@sha256:<64 hex>, as plugin.schema.json has it
IMAGE = re.compile(
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]{1,5})?(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?@sha256:[0-9a-f]{64}"
)
TARGET = re.compile(r"/[^\x00-\x1f\x7f,:]{0,254}")
VOLUME = re.compile(r"[a-z][a-z0-9_-]{0,62}")
_LINE = re.compile(r"[^\x00-\x1f\x7f]+")


def environment_name(plugin: str, name: str) -> str:
    """The name the hub registers environment ``name`` of plugin ``plugin`` under: ``sandbox/box``."""
    return f"{plugin}{SEPARATOR}{name}"


def is_plugin_environment(name: object) -> bool:
    """Whether ``name`` is the name of a plugin's environment, ``<plugin>/<name>``, rather than ``host``."""
    return isinstance(name, str) and PLUGIN_RUNTIME.fullmatch(name) is not None


def is_environment(name: object) -> bool:
    """Whether ``name`` names an environment: ``host`` or a plugin's ``<plugin>/<name>``."""
    return name == HOST or is_plugin_environment(name)


def plugin_of(environment: str) -> str | None:
    """The id of the plugin whose environment ``environment`` is; None for ``host``."""
    return environment.partition(SEPARATOR)[0] if is_plugin_environment(environment) else None


def target_problem(target: object) -> str | None:
    """Why ``target`` cannot be where a mount of an environment goes; None when it can."""
    if not isinstance(target, str) or not TARGET.fullmatch(target):
        return f"{target!r} is not an absolute path of one line, without a comma or a colon"
    parts = target.split("/")
    if target == "/" or "" in parts[1:] or "." in parts or ".." in parts:
        return f"{target!r} is / or not a normalized absolute path"
    if any(target == top or target.startswith(top + "/") for top in FORBIDDEN_TARGETS):
        return f"{target!r} is one of {', '.join(FORBIDDEN_TARGETS)} or under it"
    return None


@dataclass(frozen=True)
class Mount:
    """A mount an environment declares: ``tmpfs`` (``size_mb`` optional) or a named ``volume``."""

    type: str
    target: str
    name: str | None = None
    read_only: bool = False
    size_mb: int | None = None

    def to_json(self) -> dict:
        found: dict = {"type": self.type, "target": self.target, "read_only": self.read_only}
        if self.name is not None:
            found["name"] = self.name
        if self.size_mb is not None:
            found["size_mb"] = self.size_mb
        return found

    @classmethod
    def from_json(cls, data: object) -> Mount:
        """The mount ``to_json`` gave; ValueError when it is not one."""
        if not isinstance(data, Mapping) or data.get("type") not in MOUNT_TYPES:
            raise ValueError(f"a mount is a JSON object whose type is one of {', '.join(MOUNT_TYPES)}")
        kind, target, name, size = data["type"], data.get("target"), data.get("name"), data.get("size_mb")
        problem = target_problem(target)
        if problem:
            raise ValueError(f"a mount's target: {problem}")
        if kind == "volume" and (not isinstance(name, str) or not VOLUME.fullmatch(name) or size is not None):
            raise ValueError("a volume names itself, a-z0-9_- of at most 63 characters, and has no size")
        if kind == "tmpfs" and name is not None:
            raise ValueError("a tmpfs has no name")
        if size is not None and (isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_SIZE_MB):
            raise ValueError(f"a tmpfs's size is 1 to {MAX_SIZE_MB} MiB")
        read_only = data.get("read_only", False)
        if not isinstance(read_only, bool):
            raise ValueError("a mount's read_only is true or false")
        return cls(kind, target, name, read_only, size)


@dataclass(frozen=True)
class ContainerEnvironment:
    """An environment a plugin declared, at the version of the plugin the hub has installed: a container."""

    environment: str  # <plugin>/<name>, as the environment axis of a profile names it
    plugin: str
    version: str  # the plugin's installed version
    title: str
    image: str  # pinned by its digest
    network: str  # one of NETWORKS
    mounts: tuple[Mount, ...] = ()

    provider = "container"

    def volume_name(self, mount: Mount) -> str:
        """The engine's name of the volume ``mount`` names: ``evo-<plugin>-<name>``, so plugins never share one."""
        return f"evo-{self.plugin}-{mount.name}"

    def to_json(self) -> dict:
        return {
            "environment": self.environment,
            "plugin": self.plugin,
            "version": self.version,
            "title": self.title,
            "provider": self.provider,
            "image": self.image,
            "network": self.network,
            "mounts": [mount.to_json() for mount in self.mounts],
        }

    @classmethod
    def from_json(cls, data: object) -> ContainerEnvironment:
        """The declaration ``to_json`` gave, as the hub sends it; ValueError when it is not one."""
        if not isinstance(data, Mapping):
            raise ValueError("an environment's declaration is a JSON object")
        environment, plugin, version = data.get("environment"), data.get("plugin"), data.get("version")
        if not is_plugin_environment(environment) or plugin_of(environment) != plugin:
            raise ValueError(f"{environment!r} is not <plugin>/<name> of plugin {plugin!r}")
        if not isinstance(version, str) or not _LINE.fullmatch(version):
            raise ValueError("an environment's declaration names the plugin's version")
        if data.get("provider", cls.provider) != cls.provider:
            raise ValueError(f"an environment of a plugin is a container, not {data.get('provider')!r}")
        title = data.get("title") or environment
        if not isinstance(title, str) or len(title) > MAX_TITLE_CHARS or not _LINE.fullmatch(title):
            raise ValueError("an environment's title is one line of at most 200 characters")
        image = data.get("image")
        if not isinstance(image, str) or len(image) > MAX_IMAGE_CHARS or not IMAGE.fullmatch(image):
            raise ValueError(f"{image!r} is not an image pinned by its digest: [registry/]repository@sha256:<64 hex>")
        network = data.get("network")
        if network not in NETWORKS:
            raise ValueError(f"an environment's network is one of {', '.join(NETWORKS)}, not {network!r}")
        listed = data.get("mounts") or []
        if not isinstance(listed, list) or len(listed) > MAX_ENVIRONMENT_MOUNTS:
            raise ValueError(f"an environment has at most {MAX_ENVIRONMENT_MOUNTS} mounts")
        mounts = tuple(Mount.from_json(item) for item in listed)
        targets = [mount.target for mount in mounts]
        if len(set(targets)) != len(targets):
            raise ValueError("an environment mounts each target once")
        return cls(environment, plugin, version, title, image, network, mounts)


def container_environments(manifest: Manifest) -> list[ContainerEnvironment]:
    """The environments ``manifest`` declares, in its order, as the hub registers them."""
    return [
        ContainerEnvironment(
            environment=environment_name(manifest.id, item["name"]),
            plugin=manifest.id,
            version=manifest.version,
            title=item.get("title") or environment_name(manifest.id, item["name"]),
            image=item["image"],
            network=item["network"],
            mounts=tuple(Mount.from_json(mount) for mount in item.get("mounts") or ()),
        )
        for item in manifest.contributions("environments")
    ]
