"""The supervisor of one isolated plugin's backend: it places the bundle, builds the environment, starts the backend,
calls it with a timeout, and starts it again with a backoff when it dies or hangs (docs/plugins.md).

``Backend`` holds the plugin's name, version, bundle and the ``initialize`` payload the host gives it (its config and
what each project granted). Starting runs in a task of its own (``kick``), so placing a bundle and installing its
wheels, which may take minutes, never holds a call: ``call`` waits for a start at most its own timeout, then raises
``BackendUnavailable``, and the start goes on. Placing runs in a thread, which cancelling the start does not end:
``stop`` kills what runs under the plugin's user (pip) and waits for that thread, and a start waits for the one before,
so the host never touches a plugin's directories while a process of its user runs beside it, and a start refuses to
go on while one still does. When the host has a hub, it asks the hub for a fresh token just before
each start (``mint``) and puts it in ``initialize``, so a token lives as long as the backend it was made for and an
unchanged plugin is never restarted for a new one. ``call`` sends one request and, on ``BackendTimeout`` or
``BackendGone``, stops the backend and schedules a restart, raising ``BackendUnavailable`` so the caller answers an
error within the timeout rather than waiting. Restarts back off (``BACKOFF``), capped at ``MAX_BACKOFF``; ``status``
says the state, the process id, how many restarts, since when it runs, the last failure and when it is tried again,
for an admin to read. Whatever a start raises is a failed start that backs off, a bug of the host's included.

A backend starts only under a user of its own, which the host can give it as root (``environment.identity``). A host
that is not root refuses to start one, unless it was made with ``allow_shared_user`` for a developer's machine: then
every backend runs under the host's own user, which keeps their environments apart but not their files nor the
host's /proc (``environment``, isolation ``process``).

``Host`` holds the backends by plugin name. It never holds the hub's DSN, its secrets key or any other plugin's
secrets: a backend gets its own config in ``initialize`` and nothing else, in a process built from nothing
(``environment.backend_env``). This module imports neither the hub nor the worker.

Standard library only (asyncio).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from evo_agents.plugins.environment import (
    MAX_PLUGIN_ID,
    Identity,
    PlacementError,
    backend_command,
    backend_cwd,
    backend_env,
    build_env,
    extract,
    identity,
    isolation,
    killer_uid,
    placement,
    prepare_data,
)
from evo_agents.plugins.manifest import Manifest
from evo_agents.plugins.process import (
    STOP_GRACE,
    BackendGone,
    BackendProcess,
    BackendTimeout,
    describe_exit,
    kill_user,
    processes_of,
    reap_zombies,
)
from evo_agents.plugins.protocol import RemoteError

log = logging.getLogger(__name__)

BACKOFF = (1.0, 2.0, 5.0, 15.0, 30.0)  # seconds before the n-th restart
MAX_BACKOFF = 60.0
CALL_TIMEOUT = 30.0  # seconds one call to a backend may take
INIT_TIMEOUT = 30.0
KILL_INTERVAL = 0.5  # seconds between kills of a plugin's user while its placing winds down
STATES = ("placing", "starting", "running", "backoff")
MAX_ERROR = 2000  # characters of a failure kept in the status
MIN_HOST_TOKEN = 32  # characters of the secret the api and the plugin host share
EXAMPLE_HOST_TOKENS = frozenset({"dev-only-plugin-host-secret-change-me-000000"})  # once in deploy/hub/.env.example
SHARED_USER_REFUSED = (
    "the plugin host does not run as root, so it cannot give this backend a user of its own, and it does not run "
    "third-party code under a user it shares with the host and the other backends: run the host as root with the "
    "capabilities SETUID, SETGID, CHOWN and KILL (the plugins service of deploy/hub/docker-compose.yml), or, on a "
    "developer's machine only, start it with --allow-shared-user"
)

LINGERING = (
    "processes of plugin {name}'s user ({uid}) are still running after the host killed them; it starts no backend "
    "beside them"
)
RETIRED_LINGERING = (
    "plugin {name} is no longer installed, but {count} processes of its user ({uid}) are still running after the host "
    "killed them; it kills them again until none is left"
)
NO_KILLER_USER = (
    "plugin {name} has id {plugin_id} on the hub; the plugin host gives a user of its own only to a plugin whose id is "
    "below {limit}, so it runs no backend for this one"
)

Mint = Callable[[str], Awaitable[str]]  # a fresh hub token for the backend of the named plugin


class BackendUnavailable(RuntimeError):
    """A call could not reach a live backend: it is starting, in backoff, or just died. The caller answers an error
    within the timeout; the backend is brought back on its own schedule."""


def host_token_problem(token: str) -> str | None:
    """Why ``token`` cannot be the secret the api and the plugin host share, or None: too short, or a value written in
    an example, which anyone can read."""
    if len(token) < MIN_HOST_TOKEN:
        return f"must be at least {MIN_HOST_TOKEN} characters long"
    if token in EXAMPLE_HOST_TOKENS or "change-me" in token.lower():
        return "is the example value of deploy/hub/.env.example; set a random secret of your own"
    return None


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


async def _clean(name: str, stopping: Awaitable[bool]) -> bool:
    """What the stop of a retired plugin, or a kill of its user, says: whether no process of that user is left. An
    error counts as processes left, so the host kills them again with its backoff rather than let the error end it."""
    try:
        return await stopping
    except Exception as exc:
        log.warning(
            "plugin backend stop failed: killing its user again", extra={"contribution": name, "error": repr(exc)[:200]}
        )
        return False


class Backend:
    """One plugin's backend and its lifecycle."""

    def __init__(
        self,
        root: Path,
        manifest: Manifest,
        bundle: bytes,
        init: dict,
        *,
        mint: Mint | None = None,
        allow_shared_user: bool = False,
        after: Awaitable | None = None,
    ):
        self.root, self.manifest, self.bundle, self.init = root, manifest, bundle, init
        self.mint, self.allow_shared_user, self._after = mint, allow_shared_user, after
        self.where = placement(root, manifest.id, manifest.version)
        self.process: BackendProcess | None = None
        self.status = BackendStatus(manifest.id, manifest.version, "placing", isolation())
        self._lock = asyncio.Lock()
        self._starting: asyncio.Task | None = None
        self._building: asyncio.Future | None = None  # placing, in a thread that a cancelled start leaves running

    @property
    def name(self) -> str:
        return self.manifest.id

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.alive

    def _owner(self) -> Identity | None:
        """The user the backend runs under; PlacementError when the host can give it none and may not share its own,
        or when the plugin's id is too high for its killer to have a user of its own (``environment.killer_uid``)."""
        plugin_id = self.init.get("plugin_id", 0)
        owner = identity(plugin_id)
        if owner is None and not self.allow_shared_user:
            raise PlacementError(SHARED_USER_REFUSED)
        if owner is not None and killer_uid(owner) is None:
            raise PlacementError(NO_KILLER_USER.format(name=self.name, plugin_id=plugin_id, limit=MAX_PLUGIN_ID))
        return owner

    async def _place(self, owner: Identity | None) -> None:
        """Place the bundle, the data directory and the environment, in a thread. Cancelling the start leaves the
        thread running to its end, so it is kept: the next start and ``stop`` wait for it."""
        self.status.state = "placing"
        self._building = asyncio.ensure_future(asyncio.to_thread(self._place_now, owner))
        await asyncio.shield(self._building)

    def _place_now(self, owner: Identity | None) -> None:
        extract(self.bundle, self.where.code, owner)
        prepare_data(self.where, owner)
        build_env(self.where, self.manifest.backend.get("requirements"), owner)

    async def _replaced(self) -> None:
        """Wait for the stop of the backend this one replaces, whatever it ends with. Shielded: cancelling this start
        must not cancel that stop half way, which would leave its processes running under the same user."""
        if self._after is not None:
            await asyncio.gather(asyncio.shield(self._after), return_exceptions=True)

    async def _built(self) -> None:
        """Wait for a placing under way, whatever it ends with."""
        if self._building is not None:
            await asyncio.gather(self._building, return_exceptions=True)

    async def _wind_down(self, owner: Identity | None) -> None:
        """Wait for a placing under way to end. In isolation uid, kill what runs under the plugin's user meanwhile,
        again until it ends, so pip, even one the placing starts after the first kill, ends now, not at its timeout."""
        while owner is not None and self._building is not None and not self._building.done():
            await kill_user(owner)
            await asyncio.wait({self._building}, timeout=KILL_INTERVAL)
        await self._built()

    async def _initialize_params(self) -> dict:
        """``initialize`` as the hub gave it, with a token minted for this start when the host has a hub."""
        if self.mint is None:
            return self.init
        try:
            token = await self.mint(self.name)
        except Exception as exc:  # the hub may be down; this start fails and backs off
            raise PlacementError(f"cannot get a token for plugin {self.name} from the hub: {exc}") from None
        return {**self.init, "hub": {**self.init.get("hub", {}), "token": token}}

    async def _start(self) -> None:
        """Place and start the backend, then initialize it; PlacementError, OSError or a failed initialize raise."""
        if self.manifest.backend is None:
            raise PlacementError(f"plugin {self.name} has no backend to run")
        if self._after is not None:  # the backend this one replaces shares its user: let it stop first
            await self._replaced()
            self._after = None
        owner = self._owner()
        await self._built()  # the placing of a start that was cancelled, which runs on in its thread
        if owner is not None and not await kill_user(owner):  # nothing of an earlier start lingers under its user
            raise PlacementError(LINGERING.format(name=self.name, uid=owner.uid))
        self.reap(owner)
        await self._place(owner)
        params = await self._initialize_params()
        command = backend_command(self.where, self.manifest.backend["entry"], owner)
        process = BackendProcess(command, backend_env(self.where), backend_cwd(self.where, owner), owner)
        self.status.state = "starting"
        await process.start()
        self.process = process
        self.status.pid = process.pid
        try:
            await process.call("initialize", params, INIT_TIMEOUT)
        except (BackendTimeout, BackendGone, RemoteError) as exc:
            tail = " | ".join(list(process.stderr_tail)[-3:])
            await self._put_down(process, 2.0)
            self.process = None
            said = f"; stderr: {tail}" if tail else ""
            raise PlacementError(f"plugin {self.name} did not initialize: {exc}{said}") from None
        self.status.state = "running"
        self.status.started_at = time.time()
        self.status.error = None
        self.status.retry_at = None
        log.info("plugin backend running", extra={"contribution": self.name, "pid": process.pid})

    async def _start_once(self) -> None:
        """One start; whatever it raises is a failed start, which backs off with its cause in the status."""
        async with self._lock:
            if self.alive:
                return
            try:
                await self._start()
            except (PlacementError, OSError) as exc:
                self._schedule_retry(str(exc))
            except Exception as exc:  # a bug, or a call the event loop refuses: still a failed start, never a loop
                await self._failed_unexpectedly(exc)

    async def _failed_unexpectedly(self, exc: Exception) -> None:
        """Stop what the start spawned and back off. The traceback is logged once for as long as the cause stays the
        same, so a cause that lasts adds a warning per try, not a traceback."""
        error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR]
        if error != self.status.error:
            log.error("plugin backend start failed", exc_info=exc, extra={"contribution": self.name})
        if self.process is not None:
            process, self.process = self.process, None
            await self._put_down(process, 2.0)
        self._schedule_retry(error)

    async def _put_down(self, process: BackendProcess, grace: float) -> None:
        """Stop ``process``. Whatever its stop raises is logged and goes no further: the backend is down either way, so
        its restart backs off and the kill of its user still runs, rather than the error ending the retries."""
        try:
            await process.stop(grace=grace)
        except Exception as exc:
            log.warning("plugin backend stop failed", extra={"contribution": self.name, "error": repr(exc)[:200]})

    def reap(self, owner: Identity | None) -> None:
        """Reap the zombies of the plugin's user whose parent is the host (``process.reap_zombies``): children a
        backend made with clone(CLONE_PARENT), which nothing else waits for. Only once no placing runs, since pip,
        under that user, is the host's child too."""
        if owner is None or (self._building is not None and not self._building.done()):
            return
        reaped = reap_zombies(owner.uid)
        if reaped:
            log.info("reaped zombies of a plugin user", extra={"contribution": self.name, "count": len(reaped)})

    def in_backoff(self) -> bool:
        return self.status.retry_at is not None and time.time() < self.status.retry_at

    def kick(self) -> asyncio.Task | None:
        """Start the backend in the background unless it runs, is starting, or waits out its backoff; the start."""
        if self.alive or self.in_backoff():
            return None
        if self._starting is None or self._starting.done():
            self._starting = asyncio.create_task(self._start_once())
        return self._starting

    async def ensure(self, timeout: float = INIT_TIMEOUT) -> None:
        """Make sure a running, initialized backend is there, waiting for a start at most ``timeout`` seconds;
        BackendUnavailable when it is in backoff, still starting, or could not start."""
        if self.alive:
            return
        starting = self.kick() or self._starting
        if starting is None or (starting.done() and self.in_backoff()):
            raise BackendUnavailable(f"plugin {self.name} is in backoff until its next try: {self.status.error}")
        try:
            await asyncio.wait_for(asyncio.shield(starting), timeout)
        except TimeoutError:
            raise BackendUnavailable(f"plugin {self.name} is still starting ({self.status.state})") from None
        if not self.alive:
            raise BackendUnavailable(f"plugin {self.name} could not start: {self.status.error}")

    def _schedule_retry(self, error: str) -> None:
        self.status.restarts += 1
        self.status.state = "backoff"
        self.status.pid = None
        self.status.started_at = None
        self.status.error = error[:MAX_ERROR]
        self.status.retry_at = time.time() + _backoff(self.status.restarts - 1)
        log.warning(
            "plugin backend scheduled for restart",
            extra={"contribution": self.name, "restarts": self.status.restarts, "error": error[:200]},
        )

    async def call(self, method: str, params: dict, timeout: float = CALL_TIMEOUT):
        """One call to the backend, the start included, within ``timeout``; RemoteError for its error,
        BackendUnavailable when it is not reachable. A timeout or a dead backend stops it and schedules a restart."""
        deadline = time.monotonic() + timeout
        await self.ensure(timeout)
        process = self.process
        try:
            return await process.call(method, params, max(0.1, deadline - time.monotonic()))
        except RemoteError:
            raise
        except (BackendTimeout, BackendGone) as exc:
            async with self._lock:
                if self.process is process:  # another call may have restarted it already
                    await self._put_down(process, 2.0)
                    self.process = None
                    self._schedule_retry(f"{exc} ({describe_exit(process.returncode)})")
            raise BackendUnavailable(f"plugin {self.name} did not answer: {exc}") from None

    def user(self) -> Identity | None:
        """The plugin's user in isolation uid, whatever a start made of it; None when backends share the host's."""
        return identity(self.init.get("plugin_id", 0))

    async def stop(self) -> bool:
        """Stop the backend: a start under way is cancelled, the stop of the backend it replaced waited for, what runs
        under the plugin's user killed (pip's install included), the placing it was doing waited for, the process
        stopped, and in isolation uid every process of the plugin's user killed again and the zombies of that user the
        host is parent of reaped (``reap``). Only then may the backend that replaces this one, under the same user,
        start. False when processes of that user still run afterwards. An error of the process's stop goes no further
        than the log (``_put_down``), so the kill of the user still runs."""
        if self._starting is not None and not self._starting.done():
            self._starting.cancel()
            await asyncio.gather(self._starting, return_exceptions=True)
        await self._replaced()  # a start cancelled while it waited for it
        owner = self.user()
        await self._wind_down(owner)
        async with self._lock:
            if self.process is not None:
                await self._put_down(self.process, STOP_GRACE)
                self.status.state = "backoff"
                self.status.pid = None
                self.process = None
            if owner is not None:
                clean = await kill_user(owner)
                self.reap(owner)
                if not clean:
                    log.warning(
                        "plugin backend stopped, processes of its user remain", extra={"contribution": self.name}
                    )
                    return False
        return True


@dataclass
class Host:
    """The backends of the isolated plugins this host runs, by plugin name. ``mint`` asks the hub for a backend's
    token at each start; ``allow_shared_user`` lets a host that is not root run backends under its own user.
    ``stopping`` holds the stops of the backends ``retire`` let go of, until they end; ``lingering`` the status of
    each of those whose user still has processes after its stop, which the host kills again until none is left."""

    root: Path
    mint: Mint | None = None
    allow_shared_user: bool = False
    backends: dict[str, Backend] = field(default_factory=dict)
    stopping: dict[str, asyncio.Future] = field(default_factory=dict)
    lingering: dict[str, BackendStatus] = field(default_factory=dict)
    closing: asyncio.Event = field(default_factory=asyncio.Event)

    def place(self, manifest: Manifest, bundle: bytes, init: dict) -> Backend:
        """Register (or replace) the backend of ``manifest``; its version or ``initialize`` changing replaces it, the
        new one starting only once the old one has stopped, since both run under the plugin's user. So does a backend
        placed again while the stop of a retired one of the same plugin still runs."""
        existing = self.backends.get(manifest.id)
        stopping = self.stopping.get(manifest.id) if existing is None else None
        if existing is not None and (existing.manifest.version != manifest.version or existing.init != init):
            stopping = asyncio.ensure_future(existing.stop())
            existing = None
        if existing is None:
            existing = Backend(
                self.root,
                manifest,
                bundle,
                init,
                mint=self.mint,
                allow_shared_user=self.allow_shared_user,
                after=stopping,
            )
            self.backends[manifest.id] = existing
        return existing

    async def call(self, plugin: str, method: str, params: dict, timeout: float = CALL_TIMEOUT):
        backend = self.backends.get(plugin)
        if backend is None:
            raise BackendUnavailable(f"no backend for plugin {plugin} runs here")
        return await backend.call(method, params, timeout)

    def kick_all(self) -> None:
        """Start, in the background, every backend that is down and out of its backoff."""
        for backend in self.backends.values():
            backend.kick()

    def retire(self, name: str) -> asyncio.Future | None:
        """Forget the backend of ``name`` and stop it in the background, so the stop of one plugin, however long it
        takes, never holds the caller (the reconcile loop) nor any other plugin; the stop, or None. While processes of
        its user outlive the stop, the host kills them again, with a backoff, until /proc shows none, and its status
        says so (``lingering``); a backend placed again under that name starts only then."""
        backend = self.backends.pop(name, None)
        if backend is None:
            return None
        stopping = asyncio.ensure_future(self._retire(backend))
        self.stopping[name] = stopping
        stopping.add_done_callback(lambda done: self._stopped(name, done))
        return stopping

    async def _retire(self, backend: Backend) -> None:
        owner, tries = backend.user(), 0
        clean = await _clean(backend.name, backend.stop())
        try:
            while not clean and owner is not None:
                wait = _backoff(tries)
                tries += 1
                self.lingering[backend.name] = BackendStatus(
                    backend.name,
                    backend.manifest.version,
                    "backoff",
                    isolation(),
                    restarts=tries,
                    error=RETIRED_LINGERING.format(
                        name=backend.name, count=len(processes_of(owner.uid)), uid=owner.uid
                    ),
                    retry_at=time.time() + wait,
                )
                log.warning(
                    "processes of a retired plugin's user remain: killing them again",
                    extra={"contribution": backend.name, "uid": owner.uid, "tries": tries},
                )
                closing = self.closing.is_set()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.closing.wait(), wait)
                clean = await _clean(backend.name, kill_user(owner))
                backend.reap(owner)
                if closing:  # the host is closing: one more kill, then it leaves the rest to the container's end
                    break
        finally:
            self.lingering.pop(backend.name, None)

    def _stopped(self, name: str, done: asyncio.Future) -> None:
        if self.stopping.get(name) is done:
            del self.stopping[name]
        if not done.cancelled() and done.exception() is not None:
            log.warning("plugin backend stop failed", extra={"contribution": name, "error": repr(done.exception())})

    def status(self) -> list[dict]:
        """The state of each backend, by plugin name; where processes of a retired plugin's user still run, that, as
        nothing of that plugin starts until they are gone."""
        shown = {name: backend.status.as_dict() for name, backend in self.backends.items()}
        shown.update({name: status.as_dict() for name, status in self.lingering.items()})
        return [shown[name] for name in sorted(shown)]

    async def stop_all(self) -> None:
        self.closing.set()
        stops = [backend.stop() for backend in self.backends.values()]
        await asyncio.gather(*stops, *self.stopping.values(), return_exceptions=True)


__all__ = [
    "Backend",
    "BackendStatus",
    "BackendUnavailable",
    "Host",
    "describe_exit",
    "host_token_problem",
]
