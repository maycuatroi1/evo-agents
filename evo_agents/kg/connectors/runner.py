"""Run a connector, in process or as an external command, and stream its kg/1 messages."""

from __future__ import annotations

import shutil
import subprocess
from collections import deque
from collections.abc import Callable, Iterable, Iterator

from evo_agents.kg.connectors.base import Connector, ConnectorContext, FunctionConnector
from evo_agents.plugins.connectors import REGISTRY, ConnectorError

__all__ = ["ConnectorContext", "ConnectorError", "ConnectorRun", "connector_of", "credential_env", "load_connector"]


def load_connector(name: str) -> Callable[[ConnectorContext], Iterable[dict]]:
    """A builtin name, a ``kg_connectors`` contribution of a core plugin (entry point group evo_agents.plugins), an
    entry point in group evo_agents.kg.connectors, or ``python:module:function`` for a connector that lives next to a
    project without being packaged (``connectors.registry``)."""
    return REGISTRY.load(name)


def connector_of(ctx: ConnectorContext) -> Connector:
    """The run of the connector the source of ``ctx`` names: a Connector class runs as it is, a function through
    FunctionConnector."""
    found = load_connector(ctx.source["connector"])
    if isinstance(found, type) and issubclass(found, Connector):
        return found(ctx)
    return FunctionConnector(ctx, found)


def credential_env(source: dict, getter: Callable[[str], str | None] | None = None) -> dict[str, str]:
    """Resolve the credentials a source declares into environment variables for its connector."""
    getter = getter or evo_cred_get
    env: dict[str, str] = {}
    for entry in source.get("credentials") or []:
        if isinstance(entry, str):
            key, name = entry, entry.upper().replace(".", "_").replace("-", "_")
        else:
            key, name = entry["key"], entry["env"]
        value = getter(key)
        if not value:
            raise ConnectorError(f"credential {key!r} for source {source['id']!r} is not available")
        env[name] = value
    return env


def evo_cred_get(key: str) -> str | None:
    evo = shutil.which("evo")
    if evo is None:
        return None
    try:
        result = subprocess.run(
            [evo, "cred", "get", key], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


class ConnectorRun:
    """Iterate the messages of one connector run. After iteration, ``returncode``, ``exception`` and
    ``stderr_tail`` describe how the connector ended; a missing ``closed`` message is the checker's job."""

    def __init__(self, ctx: ConnectorContext, *, kill_after: int | None = None):
        self.ctx = ctx
        self.kill_after = kill_after
        self.connector: Connector | None = None
        self.killed = False

    @property
    def returncode(self) -> int | None:
        return self.connector.returncode if self.connector else None

    @property
    def exception(self) -> str | None:
        return self.connector.exception if self.connector else None

    @property
    def stderr_tail(self) -> deque[str]:
        return self.connector.stderr_tail if self.connector else deque(maxlen=40)

    def __iter__(self) -> Iterator[dict]:
        self.connector = connector_of(self.ctx)
        messages = iter(self.connector)
        count = 0
        try:
            for message in messages:
                if self.kill_after is not None and count >= self.kill_after:
                    self.killed = True
                    return
                count += 1
                yield message
        finally:
            close = getattr(messages, "close", None)
            if close is not None:
                close()  # an external command still running is killed
