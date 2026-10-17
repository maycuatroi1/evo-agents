"""The supervisor of one isolated plugin's backend: it places the bundle, builds the environment, starts the backend,
calls it with a timeout, and starts it again with a backoff when it dies or hangs (docs/plugins.md).

``Backend`` holds the plugin's name, version, bundle and the ``initialize`` payload the host gives it (its config, its
token and what each project granted). ``ensure`` makes sure a live backend is initialized, starting it when there is
none; ``call`` sends one request and, on ``BackendTimeout`` or ``BackendGone``, marks the backend for a restart so the
next call starts a fresh one, and raises ``BackendUnavailable`` so the caller answers an error within the timeout
rather than waiting. Restarts back off (``BACKOFF``), capped at ``MAX_BACKOFF``; ``status`` says the state, the process
id, how many restarts, since when it runs, the last failure and when it is tried again, for an admin to read.

``Host`` holds the backends by plugin name. It never holds the hub's DSN, its secrets key or any other plugin's
secrets: a backend gets its own config in ``initialize`` and nothing else, in a process built from nothing
(``environment.backend_env``), under a user of its own when the host runs as root (``environment.identity``). This
module imports neither the hub nor the worker.

Standard library only (asyncio).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from evo_agents.plugins.environment import (
    PlacementError,
    backend_command,
    backend_env,
    build_env,
    extract,
    identity,
    isolation,
    placement,
    prepare_data,
)
from evo_agents.plugins.manifest import Manifest
from evo_agents.plugins.process import BackendGone, BackendProcess, BackendTimeout, describe_exit
from evo_agents.plugins.protocol import RemoteError

log = logging.getLogger(__name__)

BACKOFF = (1.0, 2.0, 5.0, 15.0, 30.0)  # seconds before the n-th restart
MAX_BACKOFF = 60.0
CALL_TIMEOUT = 30.0  # seconds one call to a backend may take
INIT_TIMEOUT = 30.0
STATES = ("placing", "starting", "running", "backoff")


class BackendUnavailable(RuntimeError):
    """A call could not reach a live backend: it is restarting, in backoff, or just died. The caller answers an error
    within the timeout; the backend is brought back on its own schedule."""


@dataclass
class BackendStatus:
    plugin: str
    version: str
    state: str
    isolation: str
    pid: int | None = None
    restarts: int = 0
    started_at: float | None = None
    error: str | None = None
    retry_at: float | None = None

    def as_dict(self) -> dict:
        return {
            "plugin": self.plugin,
            "version": self.version,
            "state": self.state,
            "isolation": self.isolation,
            "pid": self.pid,
            "restarts": self.restarts,
            "started_at": self.started_at,
            "error": self.error,
            "retry_at": self.retry_at,
        }


def _backoff(restarts: int) -> float:
    return BACKOFF[restarts] if restarts < len(BACKOFF) else MAX_BACKOFF


class Backend:
    """One plugin's backend and its lifecycle."""

    def __init__(self, root: Path, manifest: Manifest, bundle: bytes, init: dict):
        self.root, self.manifest, self.bundle, self.init = root, manifest, bundle, init
        self.where = placement(root, manifest.id, manifest.version)
        self.process: BackendProcess | None = None
        self.status = BackendStatus(manifest.id, manifest.version, "placing", isolation())
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self.manifest.id

    async def _place(self) -> None:
        self.status.state = "placing"
        await asyncio.to_thread(extract, self.bundle, self.where.code)
        await asyncio.to_thread(build_env, self.where, self.manifest.backend.get("requirements"))
        owner = identity(self.init.get("plugin_id", 0))
        await asyncio.to_thread(prepare_data, self.where, owner)

    async def _start(self) -> None:
        """Place and start the backend, then initialize it; PlacementError, OSError or a failed initialize raise."""
        if self.manifest.backend is None:
            raise PlacementError(f"plugin {self.name} has no backend to run")
        await self._place()
        owner = identity(self.init.get("plugin_id", 0))
        command = backend_command(self.where, self.manifest.backend["entry"])
        process = BackendProcess(command, backend_env(self.where), self.where.data, owner)
        self.status.state = "starting"
        await process.start()
        self.process = process
        self.status.pid = process.pid
        try:
            await process.call("initialize", self.init, INIT_TIMEOUT)
        except (BackendTimeout, BackendGone, RemoteError) as exc:
            await process.stop(grace=2.0)
            self.process = None
            raise PlacementError(f"plugin {self.name} did not initialize: {exc}") from None
        self.status.state = "running"
        self.status.started_at = time.time()
        self.status.error = None
        self.status.retry_at = None
        log.info("plugin backend running", extra={"contribution": self.name, "pid": process.pid})

    async def ensure(self) -> None:
        """Make sure a running, initialized backend is there; BackendUnavailable when it cannot start yet."""
        async with self._lock:
            if self.process is not None and self.process.alive:
                return
            if self.status.retry_at is not None and time.time() < self.status.retry_at:
                raise BackendUnavailable(f"plugin {self.name} is in backoff until its next try")
            try:
                await self._start()
            except (PlacementError, OSError) as exc:
                self._schedule_retry(str(exc))
                raise BackendUnavailable(f"plugin {self.name} could not start: {exc}") from None

    def _schedule_retry(self, error: str) -> None:
        self.status.restarts += 1
        self.status.state = "backoff"
        self.status.pid = None
        self.status.started_at = None
        self.status.error = error[:2000]
        self.status.retry_at = time.time() + _backoff(self.status.restarts - 1)
        log.warning(
            "plugin backend scheduled for restart",
            extra={"contribution": self.name, "restarts": self.status.restarts, "error": error[:200]},
        )

    async def call(self, method: str, params: dict, timeout: float = CALL_TIMEOUT):
        """One call to the backend; RemoteError for its error, BackendUnavailable when it is not reachable. A timeout
        or a dead backend schedules a restart and raises BackendUnavailable, so no call waits past ``timeout``."""
        await self.ensure()
        process = self.process
        try:
            return await process.call(method, params, timeout)
        except RemoteError:
            raise
        except (BackendTimeout, BackendGone) as exc:
            async with self._lock:
                if self.process is process:  # another call may have restarted it already
                    await process.stop(grace=2.0)
                    self.process = None
                    self._schedule_retry(str(exc))
            raise BackendUnavailable(f"plugin {self.name} did not answer: {exc}") from None

    async def stop(self) -> None:
        async with self._lock:
            if self.process is not None:
                await self.process.stop()
                self.status.state = "backoff"
                self.status.pid = None
                self.process = None


@dataclass
class Host:
    """The backends of the isolated plugins this host runs, by plugin name."""

    root: Path
    backends: dict[str, Backend] = field(default_factory=dict)

    def place(self, manifest: Manifest, bundle: bytes, init: dict) -> Backend:
        """Register (or replace) the backend of ``manifest``; its version or config changing replaces it."""
        existing = self.backends.get(manifest.id)
        if existing is not None and (existing.manifest.version != manifest.version or existing.init != init):
            asyncio.ensure_future(existing.stop())  # noqa: RUF006 -- fire and forget the old one
            existing = None
        if existing is None:
            existing = Backend(self.root, manifest, bundle, init)
            self.backends[manifest.id] = existing
        return existing

    async def call(self, plugin: str, method: str, params: dict, timeout: float = CALL_TIMEOUT):
        backend = self.backends.get(plugin)
        if backend is None:
            raise BackendUnavailable(f"no backend for plugin {plugin} runs here")
        return await backend.call(method, params, timeout)

    def status(self) -> list[dict]:
        return [backend.status.as_dict() for backend in sorted(self.backends.values(), key=lambda b: b.name)]

    async def stop_all(self) -> None:
        await asyncio.gather(*(backend.stop() for backend in self.backends.values()), return_exceptions=True)


__all__ = ["Backend", "BackendStatus", "BackendUnavailable", "Host", "describe_exit"]
